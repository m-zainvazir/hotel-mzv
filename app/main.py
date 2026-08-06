"""The one service (plan §3).

FastAPI hosts every channel adapter; all of them drive the same brain. This is
the only thing you deploy.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

if sys.platform == "win32":
    # psycopg's async mode (Phase 4 Step 7's Postgres checkpointer) needs
    # SelectorEventLoop; Windows' default is ProactorEventLoop.
    #
    # `asyncio.set_event_loop_policy(...)` — the previous fix here — does
    # NOT work against uvicorn's own serving loop. `uvicorn.server.Server.run()`
    # calls `asyncio.run(coro, loop_factory=self.config.get_loop_factory())`,
    # and an explicit `loop_factory` makes `asyncio.run`/`asyncio.Runner`
    # build the loop directly — it never consults `asyncio.get_event_loop_policy()`
    # at all. On win32, `uvicorn.loops.asyncio.asyncio_loop_factory` hardcodes
    # `ProactorEventLoop` unless uvicorn is running as a `--reload`/multi-worker
    # subprocess (`use_subprocess=True`), which only coincidentally selects
    # `SelectorEventLoop` for an unrelated reason. Confirmed live: running
    # `uvicorn app.main:app` *without* `--reload` stalled 30s on a psycopg
    # `PoolTimeout`, then fell back to `InMemorySaver` while continuing to
    # spam "Psycopg cannot use the 'ProactorEventLoop'" warnings forever after
    # — the policy call above was silently inert.
    #
    # Fix: patch uvicorn's own factory function so it always returns
    # SelectorEventLoop on win32, regardless of `--reload`.
    # `uvicorn.loops.auto.auto_loop_factory` re-imports this name from
    # `uvicorn.loops.asyncio` fresh on every call (a function-local import,
    # not a module-level one), so patching the attribute here is picked up
    # correctly no matter how uvicorn is launched.
    #
    # Real trade-off, not just a theoretical one: Windows' asyncio subprocess
    # support (`create_subprocess_exec`, which MCP's `stdio` transport would
    # need) only works under ProactorEventLoop, never SelectorEventLoop —
    # so this closes the door on ever combining `MCP_ALLOW_STDIO=true` with
    # a Postgres checkpointer on this box; only one of the two can have the
    # loop it needs, on Windows. Linux (the deploy target) has no such
    # conflict — plain SelectorEventLoop supports subprocesses there — so
    # this is a Windows-dev-box-only limitation, and Postgres durability
    # wins the trade since it's the one already in active use.
    #
    # A standalone script that never goes through uvicorn's Server.run() at
    # all (e.g. a bare `asyncio.run(main())` with no `loop_factory`) is
    # unaffected by this — plain `asyncio.run()` *does* still consult the
    # event-loop policy, so such a script needs its own
    # `asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())`
    # call instead (see CLAUDE.md's psycopg gotcha).
    import uvicorn.loops.asyncio

    def _selector_loop_factory(use_subprocess: bool = False) -> type[asyncio.AbstractEventLoop]:
        del use_subprocess  # irrelevant here — Selector is forced unconditionally
        return asyncio.SelectorEventLoop

    uvicorn.loops.asyncio.asyncio_loop_factory = _selector_loop_factory

from app import __version__
from app.brain.graph import active_checkpointer_name, get_graph, init_postgres_checkpointer
from app.channels import admin, chat, vapi_llm, webhooks
from app.channels.ratelimit import enforce_test_session_rate_limit
from app.channels.security import is_ops_caller
from app.channels.test_links import verify_test_token
from app.config import REPO_ROOT, get_settings
from app.db.checkpointer import close_postgres_pool
from app.db.factory import get_store
from app.db.supabase_store import SupabaseStoreError
from app.logging_config import configure_logging
from app.middleware import RequestContextMiddleware
from app.preflight import verify_production_settings
from app.tenancy.admin import get_draft
from app.tenancy.loader import (
    get_repository,
    get_tenant_config,
    require_channel_enabled,
    resolve_tenant_id,
    set_repository,
)
from app.tenancy.repository import ChannelDisabledError, TenantNotFoundError
from app.tenancy.supabase_repository import SupabaseTenantRepository
from app.tools.booking.mcp_calcom import aclose_calcom_mcp_sessions
from app.tools.http_client import close_shared_clients

# At import time, not inside `lifespan`: uvicorn's own `Config.configure_logging()`
# runs before this module is imported and installs its own handlers on
# `uvicorn`/`uvicorn.access`/`uvicorn.error`, then the "Started server
# process" / "Uvicorn running on..." lines print in uvicorn's own format.
# Calling this here — during `Config.load()`'s import of the app — is what
# lets it take those loggers over before any of uvicorn's *own* startup
# lines are emitted, not just before this app's. See its docstring.
configure_logging()

WIDGET_DIST_DIR = REPO_ROOT / "widget" / "dist"
WIDGET_BUNDLE_PATH = WIDGET_DIST_DIR / "widget.js"
WIDGET_BUILDHASH_PATH = WIDGET_DIST_DIR / ".buildhash"
WIDGET_DEMO_PATH = REPO_ROOT / "widget" / "demo.html"

ADMIN_DIST_DIR = REPO_ROOT / "admin" / "dist"
ADMIN_ASSETS_DIR = ADMIN_DIST_DIR / "assets"
ADMIN_INDEX_PATH = ADMIN_DIST_DIR / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail loudly and all-at-once at startup, not one silently-open endpoint
    # at a time in production: app/channels/security.py's auth guards and
    # widget_auth's session signing fail *open* when their secret is unset —
    # correct for a zero-config dev box, wrong for a live deploy. See
    # app/preflight.py's docstring.
    settings = get_settings()
    problems = verify_production_settings(settings)
    if problems:
        raise RuntimeError(
            "APP_ENV=production preflight failed:\n" + "\n".join(f"  - {p}" for p in problems)
        )
    # LangChain's tracer reads os.environ directly, not Settings -- pydantic-
    # settings loading .env never exports there, so this must happen before
    # anything builds a traced run. Must run before get_graph(): LangGraph
    # nodes read LANGCHAIN_TRACING_V2 at call time, but the client that
    # posts runs is configured once, and getting this in before the first
    # compiled graph exists is the simplest way to guarantee that (Phase 7
    # Step 7).
    if settings.langchain_tracing_v2 and settings.langchain_api_key:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = settings.langchain_api_key
        os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project
    # Phase 8: swap the tenant repository onto Supabase *before* anything
    # else reads a tenant. content/tenants/*.json (the fallback here) is
    # still baked into the image, but is now seed + degraded-mode fallback,
    # never runtime truth in production — see plans/phase8.md "Why the
    # read-path flip is the whole phase". `refresh()` never raises, so this
    # can never fail the boot; a wholesale failure just serves the fallback
    # and shows up in /health's problems[].
    tenant_refresh_task: asyncio.Task | None = None
    if settings.tenant_source == "supabase":
        supabase_repo = SupabaseTenantRepository(fallback=get_repository())
        await supabase_repo.refresh()
        set_repository(supabase_repo)
        if settings.tenant_snapshot_refresh_seconds > 0:
            tenant_refresh_task = asyncio.create_task(
                _tenant_snapshot_refresh_loop(
                    supabase_repo, settings.tenant_snapshot_refresh_seconds
                )
            )
    # Compile the graph at boot so the first caller doesn't pay for it. This
    # always succeeds (in-memory, zero I/O) before anything Supabase-shaped
    # is attempted.
    get_graph()
    # Fail loudly at startup, not on the first booking attempt: a production
    # deploy with no SUPABASE_URL would otherwise run happily on an
    # in-memory store that loses every job on the next redeploy.
    get_store()
    # Best-effort: swap onto a durable Postgres checkpointer if DATABASE_URL
    # is configured. Never fails the boot — see its docstring.
    await init_postgres_checkpointer()
    yield
    if tenant_refresh_task is not None:
        tenant_refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tenant_refresh_task
    # Close pooled connections to Cal.com/Twilio/etc cleanly (app/tools/http_client.py).
    await close_shared_clients()
    # Close any cached Cal.com MCP sessions cleanly too — same reasoning,
    # separate cache (app/tools/booking/mcp_calcom.py).
    await aclose_calcom_mcp_sessions()
    await close_postgres_pool()


async def _tenant_snapshot_refresh_loop(repo: SupabaseTenantRepository, interval: float) -> None:
    """Background self-heal: retries after a wholesale load failure, and
    picks up an out-of-band edit (direct SQL, `scripts/sync_tenants.py`) that
    didn't go through the admin write path's explicit `refresh()` call.
    `refresh()` never raises, so there's nothing here to guard against."""
    while True:
        await asyncio.sleep(interval)
        await repo.refresh()


app = FastAPI(
    title="AI Receptionist",
    version=__version__,
    description="One LangGraph brain, two channels (voice + chat), many tenants.",
    lifespan=lifespan,
)

# Wide-open on origin, deliberately: the real per-tenant boundary is the
# origin allowlist enforced at POST /chat/session (app/channels/chat.py), not
# this middleware. Safe to leave permissive because nothing here uses cookies
# — auth is a bearer/session token in the Authorization header — so
# allow_credentials stays False, which is what makes allow_origins="*" legal.
# The Vapi and webhook routes below are server-to-server; CORS is a
# browser-enforced mechanism and doesn't apply to them regardless.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["authorization", "content-type"],
)
# Added after CORS so it's the outermost layer — its access log (and the
# X-Request-Id it echoes) then covers the whole request, CORS preflight
# included.
app.add_middleware(RequestContextMiddleware)

app.include_router(chat.router)
app.include_router(vapi_llm.router)
app.include_router(webhooks.router)
# Always mounted, unlike a naive "only include_router when ADMIN_ENABLED" —
# a router has no clean "un-include" once added, which matters because every
# test in the suite shares this one `app` object. ADMIN_ENABLED=false is
# instead enforced per-request by require_admin_enabled
# (app/channels/admin_auth.py), the first dependency on every admin route —
# functionally a 404 indistinguishable from an unmounted route.
app.include_router(admin.router)


@app.get("/widget.js", include_in_schema=False)
async def widget_bundle() -> FileResponse:
    """Serves the built widget bundle (`npm --prefix widget run build`).

    A missing bundle is a clean 404 with a pointer to the fix, not a 500 —
    the same "degrade, don't crash" posture as the rest of the channel layer.

    **`Cache-Control: no-cache`, never `immutable`.** This used to send
    `public, max-age=31536000, immutable`, which is the correct header for a
    content-hashed filename and precisely the wrong one here: this URL is
    the *frozen embed contract* (widget/README.md), so it can never gain a
    hash — the path stays `/widget.js` forever while its bytes change on
    every build. `immutable` tells a browser not to revalidate even on a
    normal reload, so a client site that loaded the widget once would keep
    serving that build for up to a year, and shipping a widget fix would
    reach nobody. The ETag below was already right and simply never got
    used, because `immutable` means the conditional request is never sent.

    `no-cache` does not mean "don't cache" — it means "cache, but
    revalidate every time". Combined with the ETag (the build hash, so it
    changes exactly when the bundle does) an unchanged bundle costs a 304
    with no body, and a changed one is picked up on the next page load.
    """
    if not WIDGET_BUNDLE_PATH.is_file():
        raise HTTPException(
            status_code=404,
            detail="widget bundle not built — run `npm --prefix widget run build` "
            "(see widget/README.md)",
        )
    headers = {"Cache-Control": "no-cache"}
    if WIDGET_BUILDHASH_PATH.is_file():
        headers["ETag"] = WIDGET_BUILDHASH_PATH.read_text(encoding="utf-8").strip()
    return FileResponse(WIDGET_BUNDLE_PATH, media_type="text/javascript", headers=headers)


@app.get("/widget/demo", include_in_schema=False)
async def widget_demo() -> FileResponse:
    """A page embedding the widget the way a client site would — the widget
    is developable against this without ever needing a real client site."""
    return FileResponse(WIDGET_DEMO_PATH, media_type="text/html")


@app.get("/health", tags=["ops"])
async def health(authenticated: bool = Depends(is_ops_caller)) -> dict:
    settings = get_settings()
    checkpointer = active_checkpointer_name()
    widget_built = WIDGET_BUNDLE_PATH.is_file()
    admin_built = ADMIN_INDEX_PATH.is_file()
    mcp_status = _mcp_health(settings)
    knowledge_status = _knowledge_health(settings)

    problems: list[str] = []
    if settings.database_url and checkpointer == "memory":
        # The only signal a bad/unreachable DATABASE_URL silently cost
        # durability — app/brain/graph.py degrades to InMemorySaver with
        # just a WARNING log line, never a crashed boot (Phase 4).
        problems.append("DATABASE_URL is set but the checkpointer fell back to memory")
    if not widget_built:
        # A deploy that forgot `npm run build` (or the Dockerfile's
        # `COPY widget/dist ...`) is one curl away from visible instead of a
        # 404 the first time a client's site tries to load the widget.
        problems.append("widget bundle not built")
    if settings.admin_enabled and not admin_built:
        # Same reasoning as widget above — only surfaced when ADMIN_ENABLED
        # is actually true, so a box that hasn't opted into the admin
        # surface at all doesn't get a spurious "problem" for a bundle it
        # was never asked to build.
        problems.append("admin bundle not built")
    if mcp_status == "unavailable":
        problems.append("mcp is enabled but langchain-mcp-adapters is not installed")
    if knowledge_status == "unavailable":
        problems.append("knowledge is enabled but SUPABASE_URL is unset")
    repository = get_repository()
    if settings.tenant_source == "supabase" and getattr(repository, "degraded", False):
        # The phantom-edit mirror image: an edit landed in Supabase, the
        # snapshot fell back to JSON, and the change looks reverted. Loud is
        # the whole point — see plans/phase8.md's "the phantom edit".
        problems.append(
            "tenant config is degraded — one or more tenants are serving the JSON "
            "fallback because the Supabase snapshot failed to load or validate"
        )

    body: dict = {
        "status": "degraded" if problems else "ok",
        "version": __version__,
        "store": "supabase" if settings.supabase_url else "memory",
        "checkpointer": checkpointer,
        "widget": "built" if widget_built else "missing",
        "admin": "built" if admin_built else "missing",
        "mcp": mcp_status,
        "knowledge": knowledge_status,
        "problems": problems,
    }
    # env / llm_provider / model / the full tenant roster are operational
    # detail, not something an anonymous caller needs — moved behind the
    # same API_AUTH_TOKEN bearer POST /chat's trusted path uses (Phase 7
    # Step 4). is_ops_caller() always returns True when no token is
    # configured, matching the dev-default fail-open convention elsewhere.
    if authenticated:
        body["env"] = settings.app_env
        body["llm_provider"] = settings.llm_provider
        body["model"] = settings.active_model
        body["tenants"] = repository.list_ids()
        body["tenant_source"] = settings.tenant_source
        body["tracing"] = bool(settings.langchain_tracing_v2 and settings.langchain_api_key)
    return body


@app.get("/readyz", tags=["ops"])
async def readyz() -> dict:
    """Unlike `/health`, this actually touches the database — Railway (or a
    keep-alive cron, Phase 7 Step 8) polling `/health` alone would never
    have stopped a free Supabase project pausing after 7 idle days, since
    no query ever left the app on that path. Kept off `/health` itself so
    Railway's frequent healthcheck polling doesn't pay a round trip to the
    database every time.
    """
    settings = get_settings()
    if not settings.supabase_url:
        return {"ready": True, "store": "memory"}

    store = get_store()
    try:
        await store.alist_jobs(settings.default_tenant_id)
    except SupabaseStoreError as exc:
        raise HTTPException(status_code=503, detail=f"database unreachable: {exc}") from exc
    return {"ready": True, "store": "supabase"}


def _mcp_health(settings) -> str:
    """`"off"` / `"json"` / `"supabase"` / `"unavailable"` — a misconfigured
    or blocked MCP layer is one curl away from visible, same reasoning as
    `store` and `widget` above."""
    if not settings.mcp_enabled:
        return "off"
    try:
        import langchain_mcp_adapters  # noqa: F401
    except ImportError:
        return "unavailable"
    return settings.mcp_source


def _knowledge_health(settings) -> str:
    """`"off"` / `"ready"` / `"unavailable"` (Phase 9 Part C). `knowledge_source`
    is `Literal["supabase"]` — there's no in-memory vector store for
    production the way `store`/`checkpointer` have one, so `KNOWLEDGE_ENABLED=true`
    with no `SUPABASE_URL` can never actually serve retrieval."""
    if not settings.knowledge_enabled:
        return "off"
    if not settings.supabase_url:
        return "unavailable"
    return "ready"


# --- Test Agent link (Phase 9.1, shared with 9.3's voice tester) -----------
#
# Not under /admin — these are the public pages a shared link actually opens,
# so the `/admin/{path:path}` catch-all ordering trap below doesn't apply to
# them regardless of where they're registered.


def _hosted_widget_page(
    tenant_name: str,
    *,
    embed_attr: str,
    label: str,
    note: str,
    heading: str | None = None,
    heading_color: str = "#e2e8f0",
    note_color: str = "#94a3b8",
) -> str:
    """One page shell for every hosted surface.

    The Test Agent preview and the public share link both render the real
    widget, differing only in which attribute identifies the tenant
    (`data-test-token` vs the frozen `data-widget-key`) and in their framing
    copy. Deliberately one function: a second bespoke chat UI is exactly
    what Phase 9.1 avoided, and two page shells would drift the same way.

    Dark, always. The widget renders dark, and a white frame around it made
    the panel look like a pasted-in screenshot rather than the thing itself.
    `color-scheme: dark` so scrollbars and form controls inside the widget's
    shadow root don't fall back to a light UA style on a black page.
    """
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{heading or f"{label} — {tenant_name}"}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    html, body {{ background: #000; }}
    body {{
      font-family: system-ui, -apple-system, sans-serif;
      margin: 0;
      padding: 2rem;
      min-height: 100vh;
      box-sizing: border-box;
      color: {heading_color};
      color-scheme: dark;
    }}
    h1 {{ font-size: 1.25rem; margin: 0 0 0.5rem; }}
    p {{ color: {note_color}; max-width: 40rem; line-height: 1.5; margin: 0; }}
  </style>
</head>
<body>
  <h1>{heading or f"{label} — {tenant_name}"}</h1>
  {f"<p>{note}</p>" if note else ""}
  <script src="/widget.js?v={_widget_build_id()}"
          {embed_attr} data-auto-open="true"></script>
</body>
</html>
"""


def _test_agent_page(tenant_name: str, token: str, *, variant: str) -> str:
    # The real widget, reused wholesale (Phase 9.1's whole point — a second,
    # bespoke test UI could drift from what a client actually sees).
    # `data-test-token` is additive to the frozen `data-widget-key` contract
    # (widget/README.md); `data-auto-open` skips the launcher click a
    # dedicated test page has no reason to require.
    is_draft = variant == "draft"
    return _hosted_widget_page(
        tenant_name,
        embed_attr=f'data-test-token="{token}"',
        label="Draft preview" if is_draft else "Test Agent",
        note=(
            "Running against the unpublished draft — re-checks it on every message, so "
            "further edits show up live in this tab. Deploy or discard and this reverts "
            "to the published bot."
            if is_draft
            else "A private, signed preview — this link isn't discoverable and expires on its own."
        ),
        # The draft/live distinction survives as an accent rather than a
        # background — amber still reads as "unpublished" against black, and
        # losing that signal would be worse than any styling win.
        heading_color="#fbbf24" if is_draft else "#e2e8f0",
        note_color="#fcd34d" if is_draft else "#94a3b8",
    )


def _widget_build_id() -> str:
    """A cache-busting query for the Test Agent page's `<script src>`.

    A real client embed can't have one — `/widget.js` with no query IS the
    frozen contract (widget/README.md) — but this page is server-rendered
    on every load, so it can, and it should: an operator testing a bot must
    never be looking at a stale bundle without knowing it.

    Earned the hard way. `/widget.js` shipped with `Cache-Control:
    immutable`, so browsers that fetched it once stopped asking entirely —
    a page load made no request at all and silently ran a months-old
    bundle, which looked exactly like a rendering bug in a brand-new
    feature and cost a long debugging session. The header is fixed now, but
    that only helps browsers whose cached copy has expired or was never
    taken; a query that changes with the build fixes it immediately and for
    anything already poisoned.
    """
    try:
        return WIDGET_BUILDHASH_PATH.read_text(encoding="utf-8").strip()[:12]
    except OSError:
        return "dev"


async def _resolve_test_tenant(claims):
    """The `TenantConfig` to show/preview with — LIVE for `variant="live"`,
    the current draft (falling back to live when there isn't one) for
    `variant="draft"`. Channel-enabled is checked against that SAME
    resolved config: previewing a draft that turns chat off should refuse
    exactly like the deployed version would, not silently ignore the change
    under test.
    """
    try:
        tenant = get_tenant_config(claims.tenant_id)
    except TenantNotFoundError as exc:
        raise HTTPException(status_code=404, detail="unknown tenant") from exc

    if claims.variant == "draft":
        draft, _ = await get_draft(claims.tenant_id)
        if draft is not None:
            tenant = draft

    try:
        require_channel_enabled(tenant, "chat")
    except ChannelDisabledError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return tenant


def _resolve_test_mode(token: str):
    claims = verify_test_token(token)
    if claims is None:
        raise HTTPException(status_code=404, detail="invalid or expired test link")
    if claims.mode == "voice":
        # Minted-and-rejected until Phase 9.3 (the voice tester — moved from
        # 9.2, which plans/phase9.2.md reassigned to flows/cards). The claim
        # shape already supports it so a link an operator hands out today
        # doesn't need to change once the voice tester ships.
        raise HTTPException(
            status_code=404, detail="voice testing is not available yet (Phase 9.3)"
        )
    return claims


@app.get("/test/{token}", include_in_schema=False)
async def test_agent_page(token: str) -> HTMLResponse:
    claims = _resolve_test_mode(token)
    tenant = await _resolve_test_tenant(claims)
    return HTMLResponse(_test_agent_page(tenant.name, token, variant=claims.variant))


@app.get("/bot/{widget_key}", include_in_schema=False)
async def shared_bot_page(widget_key: str) -> HTMLResponse:
    """The public "Share agent" link — a hosted page anyone can open.

    Deliberately NOT a Test Agent link. Those are signed, private and
    expire, which is right for an operator previewing a draft and wrong for
    a URL a client puts in an email: it would quietly stop working. This is
    addressed by the tenant's own public widget key instead, so it never
    expires, and it always serves LIVE config — a shared link must not
    follow whatever half-finished draft happens to be open.

    It's the same widget a client site embeds, on a page we host, so a
    business with no website of its own still has somewhere to point people.
    """
    try:
        tenant_id = resolve_tenant_id(widget_key=widget_key)
    except TenantNotFoundError as exc:
        raise HTTPException(status_code=404, detail="unknown bot link") from exc

    tenant = get_tenant_config(tenant_id)
    try:
        require_channel_enabled(tenant, "chat")
    except ChannelDisabledError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return HTMLResponse(
        _hosted_widget_page(
            tenant.name,
            embed_attr=f'data-widget-key="{widget_key}"',
            # A customer-facing page, so no operator framing: just the
            # business's name and the chat. `label` still drives <title>.
            label=tenant.name,
            heading=tenant.name,
            note="",
        )
    )


class TestSessionRequest(BaseModel):
    token: str


@app.post(
    "/test/session",
    include_in_schema=False,
    dependencies=[Depends(enforce_test_session_rate_limit)],
)
async def test_session(payload: TestSessionRequest) -> chat.ChatSessionResponse:
    claims = _resolve_test_mode(payload.token)
    tenant = await _resolve_test_tenant(claims)
    # No widget key at all (a tenant with an empty widget_keys[] is still
    # testable this way) and no allowed_origins check (the page is served
    # from this app's own origin) — see plans/phase9.1.md's "Feature 1b".
    # `variant` is what actually makes a "Preview draft" session re-read the
    # draft on every turn (app/brain/runner.py::stream_turn) — `tenant`
    # above only drives this handshake response's display fields.
    return await chat.start_session(tenant, widget_key="", origin=None, variant=claims.variant)


# --- serving the admin UI (Phase 8) -----------------------------------------
#
# Both of these MUST be the last routes registered in this file. Starlette
# matches routes in *registration* order and picks the first match, not the
# most specific one — `GET /admin/{path:path}` below structurally matches
# `/admin/api/session` too (`{path:path}` captures slashes), so if it were
# registered before `app.include_router(admin.router)` above, every admin API
# call would silently get back this page's HTML instead of JSON. Route
# shadowing here is exactly the kind of thing with no test unless one is
# written — see tests/test_api.py's ordering assertion.
#
# `StaticFiles(directory=...)` raises at *mount* time (i.e. at import, i.e.
# at boot) if the directory doesn't exist — turning "forgot to run `npm
# --prefix admin run build`" into a crashed boot, the opposite of
# /widget.js's deliberate 404-with-a-pointer above. Guard it explicitly
# instead; `/health`'s `admin` field is what makes the missing bundle
# visible when ADMIN_ENABLED=true and nobody built it.
if ADMIN_ASSETS_DIR.is_dir():
    app.mount("/admin/assets", StaticFiles(directory=ADMIN_ASSETS_DIR), name="admin-assets")


@app.get("/admin/{path:path}", include_in_schema=False)
async def admin_spa(path: str) -> FileResponse:
    """The SPA catch-all: every deep link (`/admin/#/tenants/hotel-mzv/config`
    — the router lives client-side in the hash, so the server never even
    sees it) renders the identical `index.html`. Not gated on
    `ADMIN_ENABLED` — that's `require_admin_enabled`'s job for the *API*;
    serving the shell itself is harmless, and the login screen it renders is
    useless without a working token regardless."""
    del path
    if not ADMIN_INDEX_PATH.is_file():
        raise HTTPException(
            status_code=404,
            detail="admin bundle not built — run `npm --prefix admin install && "
            "npm --prefix admin run build` (see admin/README.md)",
        )
    return FileResponse(ADMIN_INDEX_PATH, media_type="text/html")
