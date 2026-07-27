"""The one service (plan §3).

FastAPI hosts every channel adapter; all of them drive the same brain. This is
the only thing you deploy.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

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
from app.channels import chat, vapi_llm, webhooks
from app.config import REPO_ROOT, get_settings
from app.db.checkpointer import close_postgres_pool
from app.db.factory import get_store
from app.logging_config import configure_logging
from app.preflight import verify_production_settings
from app.tenancy.loader import get_repository
from app.tools.http_client import close_shared_clients

WIDGET_DIST_DIR = REPO_ROOT / "widget" / "dist"
WIDGET_BUNDLE_PATH = WIDGET_DIST_DIR / "widget.js"
WIDGET_BUILDHASH_PATH = WIDGET_DIST_DIR / ".buildhash"
WIDGET_DEMO_PATH = REPO_ROOT / "widget" / "demo.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
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
    # Close pooled connections to Cal.com/Twilio/etc cleanly (app/tools/http_client.py).
    await close_shared_clients()
    await close_postgres_pool()


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

app.include_router(chat.router)
app.include_router(vapi_llm.router)
app.include_router(webhooks.router)


@app.get("/widget.js", include_in_schema=False)
async def widget_bundle() -> FileResponse:
    """Serves the built widget bundle (`npm --prefix widget run build`).

    A missing bundle is a clean 404 with a pointer to the fix, not a 500 —
    the same "degrade, don't crash" posture as the rest of the channel layer.
    """
    if not WIDGET_BUNDLE_PATH.is_file():
        raise HTTPException(
            status_code=404,
            detail="widget bundle not built — run `npm --prefix widget run build` "
            "(see widget/README.md)",
        )
    headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    if WIDGET_BUILDHASH_PATH.is_file():
        headers["ETag"] = WIDGET_BUILDHASH_PATH.read_text(encoding="utf-8").strip()
    return FileResponse(WIDGET_BUNDLE_PATH, media_type="text/javascript", headers=headers)


@app.get("/widget/demo", include_in_schema=False)
async def widget_demo() -> FileResponse:
    """A page embedding the widget the way a client site would — the widget
    is developable against this without ever needing a real client site."""
    return FileResponse(WIDGET_DEMO_PATH, media_type="text/html")


@app.get("/health", tags=["ops"])
async def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "env": settings.app_env,
        "llm_provider": settings.llm_provider,
        "model": settings.active_model,
        "tenants": get_repository().list_ids(),
        "store": "supabase" if settings.supabase_url else "memory",
        "checkpointer": active_checkpointer_name(),
        # A deploy that forgot `npm run build` (or the Dockerfile's
        # `COPY widget/dist ...`) is one curl away from visible instead of a
        # 404 the first time a client's site tries to load the widget.
        "widget": "built" if WIDGET_BUNDLE_PATH.is_file() else "missing",
        "mcp": _mcp_health(settings),
    }


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
