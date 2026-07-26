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
    # psycopg's async mode (Phase 4 Step 7's Postgres checkpointer) cannot
    # run on Windows' default ProactorEventLoop — only SelectorEventLoop.
    # Linux (the actual deploy target) is unaffected; this only matters for
    # local `uvicorn --reload` on a Windows dev box. Must be set before
    # uvicorn creates its event loop, hence top-of-module.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app import __version__
from app.brain.graph import active_checkpointer_name, get_graph, init_postgres_checkpointer
from app.channels import chat, vapi_llm, webhooks
from app.config import REPO_ROOT, get_settings
from app.db.checkpointer import close_postgres_pool
from app.db.factory import get_store
from app.logging_config import configure_logging
from app.tenancy.loader import get_repository
from app.tools.http_client import close_shared_clients

WIDGET_DIST_DIR = REPO_ROOT / "widget" / "dist"
WIDGET_BUNDLE_PATH = WIDGET_DIST_DIR / "widget.js"
WIDGET_BUILDHASH_PATH = WIDGET_DIST_DIR / ".buildhash"
WIDGET_DEMO_PATH = REPO_ROOT / "widget" / "demo.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
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
    }
