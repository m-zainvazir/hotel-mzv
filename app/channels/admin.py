"""Admin dashboard API (Phase 8).

Same-origin only, deliberately: `app/main.py`'s CORS `allow_methods` is
`GET`/`POST`/`OPTIONS`, and widening it on a wildcard origin to support
`PUT`/`PATCH`/`DELETE` would extend a config-mutating surface to every
origin on the internet, defended by a bearer alone. The admin UI (`admin/`)
is served from this same app (Step 8), so no CORS preflight is ever
involved for it.

Every route depends on `require_admin` or `require_tenant_access` — never
the raw token, and every tenant-scoped route takes `tenant_id` from the URL
path, never the query string or body. That's what makes the later flip to
real per-tenant login additive: see `app/channels/admin_auth.py`'s
docstring.

The write route (`PUT /tenants/{tenant_id}`) is the one exception to
"reads only": it calls straight through to `app/tenancy/admin.py::save_tenant`,
kept in a separate module because it uses the Supabase **secret** key (an
operator/backend action), while every read here uses the tenant-scoped JWT
`AnalyticsStore` already mints. Mixing the two credentials in one file is
exactly the kind of thing that gets copy-pasted wrong later.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, status
from pydantic import ValidationError

from app.brain.prompts.system import render_system_prompt
from app.channels.admin_auth import (
    AdminPrincipal,
    require_admin,
    require_admin_enabled,
    require_tenant_access,
)
from app.channels.ratelimit import enforce_admin_rate_limit
from app.db.factory import get_store
from app.tenancy import admin as tenancy_admin
from app.tenancy.loader import get_repository, get_tenant_config
from app.tenancy.models import TenantConfig
from app.tenancy.repository import TenantNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/api",
    tags=["admin"],
    # Order matters: ADMIN_ENABLED gates first (a disabled surface 404s
    # before spending a rate-limit slot or inspecting a bearer), then the
    # rate limiter, then each route's own require_admin/require_tenant_access.
    dependencies=[Depends(require_admin_enabled), Depends(enforce_admin_rate_limit)],
)

#: Default analytics window when the caller doesn't specify one.
_DEFAULT_WINDOW_DAYS = 30


def _accessible_tenant_ids(principal: AdminPrincipal) -> list[str]:
    all_ids = get_repository().list_ids()
    if principal.tenant_ids is None:
        return all_ids
    return [tenant_id for tenant_id in all_ids if tenant_id in principal.tenant_ids]


def _config_health(config: TenantConfig) -> dict:
    """Costs nothing beyond what we already hold — no store read, no
    network call — and is the single most useful panel for an operator:
    every field here is a "did we forget to flip this live" question."""
    return {
        "booking_provider": config.booking.provider,
        "booking_is_live": config.booking.provider != "stub",
        "notifications_provider": config.notifications.provider,
        "notifications_is_live": config.notifications.provider != "stub",
        "vapi_assistant_configured": bool(config.vapi.assistant_id),
        "chat_allowed_origins_empty": not config.chat.allowed_origins,
        "warm_transfer_enabled": config.emergency.allow_warm_transfer,
        "mcp_servers_enabled": sum(1 for server in config.mcp_servers if server.enabled),
    }


@router.get("/session")
async def get_session(principal: AdminPrincipal = Depends(require_admin)) -> dict:
    """Who am I, which tenants, what may I do — the UI branches on this
    response, never on a hardcoded "am I operator". The tenant-login build
    is the same bundle with a different response here."""
    return {
        "kind": principal.kind,
        "tenant_ids": list(principal.tenant_ids) if principal.tenant_ids is not None else None,
        "capabilities": ["read", "write"] if principal.kind == "operator" else ["read"],
    }


@router.get("/tenants")
async def list_tenants(principal: AdminPrincipal = Depends(require_admin)) -> dict:
    return {"tenant_ids": _accessible_tenant_ids(principal)}


@router.get("/overview")
async def get_overview(principal: AdminPrincipal = Depends(require_admin)) -> dict:
    """The operator landing page: a per-tenant loop over each tenant's own
    metrics, never the secret key (plans/phase8.md's "cross-tenant rollups"
    decision). A single tenant's failure degrades that one row instead of
    failing the whole page — one Supabase hiccup shouldn't blank the
    dashboard for every tenant."""
    store = get_store()
    until = date.today()
    since = until - timedelta(days=_DEFAULT_WINDOW_DAYS)

    rows = []
    for tenant_id in _accessible_tenant_ids(principal):
        try:
            config = get_tenant_config(tenant_id)
            metrics = await store.atenant_metrics(tenant_id, since=since, until=until)
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "name": config.name,
                    "trade": config.trade,
                    "status": config.status,
                    "metrics": metrics.model_dump(mode="json"),
                }
            )
        except Exception:
            logger.warning("overview row failed for tenant %r", tenant_id, exc_info=True)
            rows.append({"tenant_id": tenant_id, "error": "failed to load — see server logs"})
    return {"tenants": rows}


@router.get("/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: str, principal: AdminPrincipal = Depends(require_tenant_access)
) -> dict:
    try:
        config = get_tenant_config(tenant_id)
    except TenantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    body = config.model_dump(mode="json")
    body["_health"] = _config_health(config)
    # The fully-resolved prompt the brain actually sends to the model right
    # now — computed here (string formatting only, no store/network I/O) so
    # the admin panel's "AI Prompt" tab has a real starting point to edit
    # from, not a blank box, on a tenant with no override set yet.
    body["_rendered_system_prompt"] = render_system_prompt(config, channel="chat")
    # None under TENANT_SOURCE=json (dev/test default) — there's no
    # `tenants.updated_at` row to version against. PUT reads None as "skip
    # the optimistic-concurrency check", matching that mode's reality: JSON
    # dev editing has no concurrent-writer story to protect against.
    body["_version"] = await tenancy_admin.get_tenant_version(tenant_id)
    return body


def _validation_errors(exc: ValidationError) -> list[dict]:
    return [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]


@router.put("/tenants/{tenant_id}")
async def put_tenant(
    tenant_id: str,
    payload: dict[str, Any] = Body(...),
    principal: AdminPrincipal = Depends(require_tenant_access),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict:
    """Whole-document save: fetch current -> merge the request body onto it
    -> `TenantConfig.model_validate` -> save.

    The merge is a SHALLOW top-level merge (`{**current, **payload}`), not a
    deep one: submitting `{"greeting": "..."}` changes just that scalar, but
    submitting `{"voice": {"speed": 1.2}}` replaces the *entire* `voice`
    section, resetting any other field in it to `VoiceSettings`'s defaults.
    Deliberate — the admin UI always holds and submits a section's complete
    current state, never a sparse delta within it, and a real deep-merge has
    its own footguns (how do you remove a list element, or set a field back
    to null?). Pydantic's own validators
    (`_calcom_tenants_declare_event_types`, `_unique_service_slugs`, every
    `Field(gt=..., le=...)`, ...) are the entire validation layer — this
    route's only job is mapping `ValidationError` to a 422 whose `loc`
    tuples a UI can attach to the right input, and mapping the two things
    Pydantic can't know about (a stale version, a missing voice consent) to
    409s with actionable copy.
    """
    try:
        current = get_tenant_config(tenant_id)
    except TenantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    merged = {**current.model_dump(mode="json"), **payload, "tenant_id": tenant_id}
    try:
        proposed = TenantConfig.model_validate(merged)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_validation_errors(exc),
        ) from exc

    if principal.kind != "operator":
        violations = tenancy_admin.operator_only_violations(current, proposed)
        if violations:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"only an operator principal may change: {', '.join(violations)}",
            )

    try:
        saved = await tenancy_admin.save_tenant(proposed, expected_version=if_match)
    except tenancy_admin.VersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except tenancy_admin.VoiceConsentRequiredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    body = saved.model_dump(mode="json")
    body["_health"] = _config_health(saved)
    body["_rendered_system_prompt"] = render_system_prompt(saved, channel="chat")
    body["_version"] = await tenancy_admin.get_tenant_version(tenant_id)
    return body


@router.get("/tenants/{tenant_id}/metrics")
async def get_tenant_metrics(
    tenant_id: str,
    principal: AdminPrincipal = Depends(require_tenant_access),
    since: date | None = Query(default=None, alias="from"),
    until: date | None = Query(default=None, alias="to"),
) -> dict:
    resolved_until = until or date.today()
    resolved_since = since or (resolved_until - timedelta(days=_DEFAULT_WINDOW_DAYS))
    store = get_store()
    totals = await store.atenant_metrics(tenant_id, since=resolved_since, until=resolved_until)
    series = await store.adaily_series(tenant_id, since=resolved_since, until=resolved_until)
    return {
        "since": resolved_since.isoformat(),
        "until": resolved_until.isoformat(),
        "totals": totals.model_dump(mode="json"),
        "daily": [day.model_dump(mode="json") for day in series],
    }


@router.get("/tenants/{tenant_id}/calls")
async def list_calls(
    tenant_id: str,
    principal: AdminPrincipal = Depends(require_tenant_access),
    limit: int = Query(default=50, gt=0, le=200),
) -> dict:
    """`CallSummary`, not `Call` — no transcript in this response, at any
    layer (SupabaseStore excludes it in the SQL `select=` itself)."""
    calls = await get_store().alist_recent_calls(tenant_id, limit=limit)
    return {"calls": [call.model_dump(mode="json") for call in calls]}


@router.get("/tenants/{tenant_id}/calls/{call_id}")
async def get_call(
    tenant_id: str, call_id: str, principal: AdminPrincipal = Depends(require_tenant_access)
) -> dict:
    """The only route that reaches a transcript — one call at a time, on an
    explicit operator action."""
    call = await get_store().aget_call(tenant_id, call_id)
    if call is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no call {call_id!r} for tenant {tenant_id!r}",
        )
    return call.model_dump(mode="json")


@router.get("/tenants/{tenant_id}/chats")
async def list_chats(
    tenant_id: str,
    principal: AdminPrincipal = Depends(require_tenant_access),
    limit: int = Query(default=50, gt=0, le=200),
) -> dict:
    sessions = await get_store().alist_chat_sessions(tenant_id, limit=limit)
    return {"sessions": [session.model_dump(mode="json") for session in sessions]}


@router.get("/tenants/{tenant_id}/chats/{session_id}")
async def get_chat_messages(
    tenant_id: str,
    session_id: str,
    principal: AdminPrincipal = Depends(require_tenant_access),
) -> dict:
    store = get_store()
    session = await store.aget_chat_session(tenant_id, session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no chat session {session_id!r} for tenant {tenant_id!r}",
        )
    messages = await store.alist_chat_messages(tenant_id, session_id)
    return {
        "session": session.model_dump(mode="json"),
        "messages": [message.model_dump(mode="json") for message in messages],
    }


@router.get("/tenants/{tenant_id}/jobs")
async def list_jobs(
    tenant_id: str,
    principal: AdminPrincipal = Depends(require_tenant_access),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
) -> dict:
    jobs = await get_store().alist_jobs(tenant_id, since=since, until=until)
    return {"jobs": [job.model_dump(mode="json") for job in jobs]}


@router.get("/tenants/{tenant_id}/escalations")
async def list_escalations(
    tenant_id: str, principal: AdminPrincipal = Depends(require_tenant_access)
) -> dict:
    escalations = await get_store().alist_escalations(tenant_id)
    return {"escalations": [escalation.model_dump(mode="json") for escalation in escalations]}
