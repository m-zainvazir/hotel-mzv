"""In-process rate limiting for the public chat surface (Phase 7 Step 6).

Fixed-window counters in a dict behind an `RLock` — the same shape as
`app/tenancy/loader.py`'s TTL cache. No new dependency: this is a ceiling
against one abusive caller burning a tenant's LLM/Cal.com budget, not the
shared usage-tier model `plans/phase10.md` defers.

**Per-replica only** — see `plans/phase7.md`'s "Decisions locked": correct
at the single replica this app deploys as (see the Dockerfile's own
single-worker comment), silently too permissive the moment there's a
second one. Applied only to `POST /chat` and `POST /chat/session` — never
`/chat/completions` or `/webhooks/vapi`, which are already gated by
`VAPI_WEBHOOK_SECRET` and where throttling a live phone call is worse than
the spend. A *trusted* `/chat` caller (the shared `API_AUTH_TOKEN` bearer —
`chat_cli`, tests, another backend) is exempt for the same reason: it
already holds the same class of secret those routes are gated by.

Three settings, three scopes, each doing a different job:
  * `chat_requests_per_minute` — a burst cap, per client IP, on both routes.
  * `session_requests_per_hour` — per widget session, on `POST /chat` only:
    stops one conversation from looping or retry-storming.
  * `chat_requests_per_day` — per tenant, on `POST /chat` only: protects the
    tenant's own daily LLM budget (Groq's free tier is ~76 requests/day —
    see the README's cost section) from any one widget key.
"""

from __future__ import annotations

from threading import RLock
from time import monotonic as _monotonic

from fastapi import Depends, HTTPException, Request, status

from app.channels.security import ChatAuth, require_chat_caller
from app.config import get_settings

_lock = RLock()
#: (scope, key) -> (window_start_monotonic, count)
_windows: dict[tuple[str, str], tuple[float, int]] = {}


def reset_rate_limits() -> None:
    """Test hook — drop every window so a test starts unthrottled."""
    with _lock:
        _windows.clear()


def _hit(scope: str, key: str, *, limit: int, window_seconds: float) -> float | None:
    """Records one hit for `(scope, key)`; returns seconds-until-reset if
    this hit is over `limit`, else `None`."""
    now = _monotonic()
    with _lock:
        window_key = (scope, key)
        start, count = _windows.get(window_key, (now, 0))
        if now - start >= window_seconds:
            start, count = now, 0
        count += 1
        _windows[window_key] = (start, count)
        if count > limit:
            return max(0.0, window_seconds - (now - start))
    return None


def _client_ip(request: Request) -> str:
    # Relies on uvicorn's `--proxy-headers` (infra/Dockerfile) having
    # already rewritten `request.client` from X-Forwarded-For — this module
    # never parses that header itself.
    return request.client.host if request.client else "unknown"


def _too_many_requests(retry_after: float) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="rate limit exceeded",
        headers={"Retry-After": str(max(1, int(retry_after) + 1))},
    )


async def enforce_session_rate_limit(request: Request) -> None:
    """`POST /chat/session` — no session exists yet at this point, so the
    client IP is the only identity to key on."""
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return
    retry_after = _hit(
        "chat-ip",
        _client_ip(request),
        limit=settings.chat_requests_per_minute,
        window_seconds=60.0,
    )
    if retry_after is not None:
        raise _too_many_requests(retry_after)


async def enforce_chat_rate_limit(
    request: Request,
    auth: ChatAuth = Depends(require_chat_caller),
) -> None:
    """`POST /chat` — exempt for a *trusted* caller (server-to-server,
    already holds `API_AUTH_TOKEN`); enforced for a widget-token caller.

    Depends on `require_chat_caller` directly rather than re-deriving
    tenant/session identity: FastAPI caches a dependency's result per
    request by default, so this doesn't run auth twice — the route
    function's own `Depends(require_chat_caller)` reuses the same call.
    """
    if auth.mode != "widget":
        return
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return

    assert auth.claims is not None

    ip_retry = _hit(
        "chat-ip",
        _client_ip(request),
        limit=settings.chat_requests_per_minute,
        window_seconds=60.0,
    )
    if ip_retry is not None:
        raise _too_many_requests(ip_retry)

    session_retry = _hit(
        "chat-session",
        f"{auth.claims.tenant_id}:{auth.claims.session_id}",
        limit=settings.session_requests_per_hour,
        window_seconds=3600.0,
    )
    if session_retry is not None:
        raise _too_many_requests(session_retry)

    tenant_retry = _hit(
        "chat-tenant",
        auth.claims.tenant_id,
        limit=settings.chat_requests_per_day,
        window_seconds=86400.0,
    )
    if tenant_retry is not None:
        raise _too_many_requests(tenant_retry)


async def enforce_admin_rate_limit(request: Request) -> None:
    """`/admin/api/*` (Phase 8) — a generous per-IP ceiling on top of
    `require_admin`'s auth check. Not exempt for anyone, unlike the chat
    routes' trusted-caller exemption: there the exemption applies because a
    trusted caller already holds the same class of secret those routes are
    gated by, but here the admin bearer itself *is* that secret — nothing
    to exempt it from. `/admin/api/overview` fans out N per-tenant round
    trips per call, and the analytics endpoints query the same free-tier
    Supabase project that answers phone calls, so this exists to stop a
    dashboard left open on auto-refresh from starving booking traffic."""
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return
    retry_after = _hit(
        "admin-ip",
        _client_ip(request),
        limit=settings.admin_requests_per_minute,
        window_seconds=60.0,
    )
    if retry_after is not None:
        raise _too_many_requests(retry_after)
