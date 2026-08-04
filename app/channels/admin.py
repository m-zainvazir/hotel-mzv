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

import asyncio
import json
import logging
import secrets
from datetime import date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, ValidationError

from app.brain.prompts.system import render_system_prompt
from app.channels.admin_auth import (
    AdminPrincipal,
    require_admin,
    require_admin_enabled,
    require_tenant_access,
)
from app.channels.ratelimit import enforce_admin_rate_limit
from app.config import get_settings
from app.db.factory import get_store
from app.tenancy import admin as tenancy_admin
from app.tenancy.loader import get_repository, get_tenant_config
from app.tenancy.models import TenantConfig
from app.tenancy.repository import TenantNotFoundError
from app.tenancy.sync import TenantSyncError

logger = logging.getLogger(__name__)

#: Seeded from the two original tenants' shapes plus three new ones
#: (Step B4) — content/templates/<name>.json, each a full TenantConfig
#: with identity fields (phone_numbers, widget_keys, vapi, voice_id,
#: event_type_id) left blank/null for the operator to fill in for real.
_TEMPLATE_NAMES = frozenset({"hotel", "clinic", "trades", "salon", "restaurant"})

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
    ids = _accessible_tenant_ids(principal)
    # `tenants` is additive, not a replacement for `tenant_ids` — cheap
    # (repository reads only, no store/network I/O) and lets the sidebar
    # (Phase 9 Part B) group archived bots without a second round trip.
    tenants = []
    for tenant_id in ids:
        try:
            config = get_tenant_config(tenant_id)
            tenants.append({"tenant_id": tenant_id, "name": config.name, "status": config.status})
        except TenantNotFoundError:
            tenants.append({"tenant_id": tenant_id, "name": tenant_id, "status": "unknown"})
    return {"tenant_ids": ids, "tenants": tenants}


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


async def _tenant_detail(config: TenantConfig) -> dict:
    """The response shape `get_tenant`/`put_tenant`/the Step B4 lifecycle
    routes all return — factored out so a created/archived/restored tenant
    renders in the panel exactly like any other GET would."""
    body = config.model_dump(mode="json")
    body["_health"] = _config_health(config)
    body["_rendered_system_prompt"] = render_system_prompt(config, channel="chat")
    body["_version"] = await tenancy_admin.get_tenant_version(config.tenant_id)
    return body


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


# --- bot lifecycle (Phase 9 Part B) -----------------------------------------


class CreateTenantRequest(BaseModel):
    """The panel's minimal "+ New bot" form — everything else on
    `TenantConfig` gets Pydantic's own defaults (blank mode) or the chosen
    template's/source tenant's values (template/clone mode). An operator
    fine-tunes the rest afterward through the normal `PUT` editor."""

    mode: Literal["blank", "template", "clone"]
    template: str | None = None
    source_tenant_id: str | None = None
    tenant_id: str
    name: str
    trade: str
    greeting: str
    escalation_phone: str


class PurgeTenantRequest(BaseModel):
    #: Typed confirmation — must equal the path's tenant_id. Plan §9 Risk 5:
    #: purge is the most destructive operation in the codebase, so this is a
    #: deliberate second keystroke, not just a "are you sure?" click.
    tenant_id: str


def _load_template(name: str) -> dict[str, Any]:
    if name not in _TEMPLATE_NAMES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown template {name!r} — choose one of {sorted(_TEMPLATE_NAMES)}",
        )
    path = get_settings().content_dir / "templates" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _clone_base(source_tenant_id: str) -> dict[str, Any]:
    """The source tenant's full config, with everything account-specific
    cleared — plan §9 Step B4's exact list: `tenant_id` (overwritten by the
    caller below regardless), `phone_numbers`, `widget_keys`,
    `vapi.assistant_id`, `voice.voice_id`, `booking.event_type_id`."""
    try:
        source = get_tenant_config(source_tenant_id)
    except TenantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown source_tenant_id {source_tenant_id!r}",
        ) from exc
    base = source.model_dump(mode="json")
    base["phone_numbers"] = []
    base["widget_keys"] = []
    base["vapi"] = {**base["vapi"], "assistant_id": None}
    base["voice"] = {**base["voice"], "voice_id": None}
    base["booking"] = {**base["booking"], "event_type_id": None}
    return base


def _generate_widget_key() -> str:
    return f"pk_widget_{secrets.token_hex(12)}"


@router.post("/tenants", status_code=status.HTTP_201_CREATED)
async def create_tenant_route(
    payload: CreateTenantRequest, principal: AdminPrincipal = Depends(require_admin)
) -> dict:
    """Create a bot from blank, a template, or a clone of an existing one.

    Operator-only (unlike every tenant-scoped route below, this can't use
    `require_tenant_access` — there's no existing tenant to scope access to
    yet), so this behaves correctly the day tenant login lands with no
    re-audit, matching every other operator-only path in this module.
    """
    if principal.kind != "operator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only an operator principal may create a tenant",
        )

    if payload.mode == "template":
        if not payload.template:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="template is required when mode='template'",
            )
        base = _load_template(payload.template)
    elif payload.mode == "clone":
        if not payload.source_tenant_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="source_tenant_id is required when mode='clone'",
            )
        base = _clone_base(payload.source_tenant_id)
    else:
        base = {}

    # Emergency is merged one level deeper than everything else here — a
    # blind top-level overwrite (matching put_tenant's own shallow-merge
    # convention) would drop a template's/source's own danger keywords and
    # holding message, keeping only the operator-supplied escalation_phone.
    merged = {
        **base,
        "tenant_id": payload.tenant_id,
        "name": payload.name,
        "trade": payload.trade,
        "greeting": payload.greeting,
        "emergency": {
            **(base.get("emergency") or {}),
            "escalation_phone": payload.escalation_phone,
        },
        "widget_keys": [_generate_widget_key()],
        "status": "active",
    }
    try:
        proposed = TenantConfig.model_validate(merged)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_validation_errors(exc),
        ) from exc

    try:
        saved = await tenancy_admin.create_tenant(proposed)
    except tenancy_admin.TenantAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except TenantSyncError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return await _tenant_detail(saved)


@router.post("/tenants/{tenant_id}/archive")
async def archive_tenant(
    tenant_id: str, principal: AdminPrincipal = Depends(require_tenant_access)
) -> dict:
    try:
        updated = await tenancy_admin.set_tenant_status(tenant_id, "archived")
    except TenantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return await _tenant_detail(updated)


@router.post("/tenants/{tenant_id}/restore")
async def restore_tenant(
    tenant_id: str, principal: AdminPrincipal = Depends(require_tenant_access)
) -> dict:
    try:
        updated = await tenancy_admin.set_tenant_status(tenant_id, "active")
    except TenantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return await _tenant_detail(updated)


@router.post("/tenants/{tenant_id}/purge")
async def purge_tenant_route(
    tenant_id: str,
    payload: PurgeTenantRequest = Body(...),
    principal: AdminPrincipal = Depends(require_admin),
) -> dict:
    """Irreversible. Operator-only (same reasoning as create), refuses
    unless the typed confirmation in the body matches the path's tenant_id,
    and delegates the archived-status precondition + FK-ordered deletes to
    `app/tenancy/admin.py::purge_tenant` — see that function's docstring for
    the full order and what's best-effort vs. what aborts."""
    if principal.kind != "operator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only an operator principal may purge a tenant",
        )
    if payload.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="confirmation tenant_id does not match — type the exact tenant id to confirm",
        )

    try:
        counts = await tenancy_admin.purge_tenant(tenant_id)
    except TenantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except tenancy_admin.TenantNotArchivedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except tenancy_admin.TenantPurgeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    logger.info("tenant %s purged by operator: %s", tenant_id, counts)
    return {"tenant_id": tenant_id, "deleted": counts}


# --- knowledge base / RAG (Phase 9 Part C) ----------------------------------


def require_knowledge_enabled() -> None:
    """Mirrors `require_admin_enabled` — `KNOWLEDGE_ENABLED=false` (the
    default) means every route below 404s before doing any real work,
    indistinguishable from the routes never having existed."""
    if not get_settings().knowledge_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


class KnowledgeTextRequest(BaseModel):
    title: str = ""
    text: str


class KnowledgeUrlRequest(BaseModel):
    url: str
    crawl: bool = False
    max_pages: int = 20
    max_depth: int = 2


class KnowledgeSearchRequest(BaseModel):
    query: str


@router.get("/tenants/{tenant_id}/knowledge")
async def list_knowledge(
    tenant_id: str,
    principal: AdminPrincipal = Depends(require_tenant_access),
    _enabled: None = Depends(require_knowledge_enabled),
) -> dict:
    documents = await get_store().alist_documents(tenant_id)
    return {"documents": [d.model_dump(mode="json") for d in documents]}


@router.post("/tenants/{tenant_id}/knowledge/text")
async def add_knowledge_text(
    tenant_id: str,
    payload: KnowledgeTextRequest,
    principal: AdminPrincipal = Depends(require_tenant_access),
    _enabled: None = Depends(require_knowledge_enabled),
) -> dict:
    from app.rag.ingest import start_ingestion_from_text

    if not payload.text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="text must not be empty"
        )
    tenant = get_tenant_config(tenant_id)
    document = await start_ingestion_from_text(
        get_store(), tenant, title=payload.title, text=payload.text
    )
    return document.model_dump(mode="json")


@router.post("/tenants/{tenant_id}/knowledge/upload")
async def upload_knowledge_files(
    tenant_id: str,
    files: list[UploadFile] = File(...),
    principal: AdminPrincipal = Depends(require_tenant_access),
    _enabled: None = Depends(require_knowledge_enabled),
) -> dict:
    """Multiple files, each queued independently — one bad file must never
    block the others (same "one failure degrades, never the whole batch"
    posture `app/mcp/client.py` already established for MCP servers)."""
    from app.rag.ingest import start_ingestion_from_file

    tenant = get_tenant_config(tenant_id)
    settings = get_settings()
    store = get_store()
    results = []
    for file in files:
        data = await file.read()
        if len(data) > settings.knowledge_max_upload_bytes:
            results.append(
                {
                    "title": file.filename,
                    "status": "failed",
                    "error": f"exceeds the {settings.knowledge_max_upload_bytes}-byte upload limit",
                }
            )
            continue
        document = await start_ingestion_from_file(
            store, tenant, filename=file.filename or "upload", data=data
        )
        results.append(document.model_dump(mode="json"))
    return {"documents": results}


@router.post("/tenants/{tenant_id}/knowledge/url")
async def add_knowledge_url(
    tenant_id: str,
    payload: KnowledgeUrlRequest,
    principal: AdminPrincipal = Depends(require_tenant_access),
    _enabled: None = Depends(require_knowledge_enabled),
) -> dict:
    from app.rag.ingest import start_ingestion_from_url

    tenant = get_tenant_config(tenant_id)
    documents = await start_ingestion_from_url(
        get_store(),
        tenant,
        url=payload.url,
        crawl=payload.crawl,
        max_pages=payload.max_pages,
        max_depth=payload.max_depth,
    )
    return {"documents": [d.model_dump(mode="json") for d in documents]}


@router.post("/tenants/{tenant_id}/knowledge/{document_id}/reindex")
async def reindex_knowledge_document(
    tenant_id: str,
    document_id: str,
    principal: AdminPrincipal = Depends(require_tenant_access),
    _enabled: None = Depends(require_knowledge_enabled),
) -> dict:
    """Only a `source_type == "url"` document can be re-indexed — the raw
    source is never persisted for a pasted-text or uploaded-file document
    (only its already-chunked, already-embedded content is), so those need
    a fresh paste/upload instead, exactly like a brand-new document."""
    import httpx

    from app.rag.crawl import CrawlError, fetch_page
    from app.rag.ingest import ingest_text

    store = get_store()
    document = await store.aget_document(tenant_id, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no knowledge document {document_id!r}"
        )
    if document.source_type != "url" or not document.source_ref:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="only a URL-sourced document can be re-indexed — re-paste or re-upload instead",
        )

    tenant = get_tenant_config(tenant_id)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            page = await fetch_page(client, document.source_ref)
    except CrawlError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    asyncio.create_task(ingest_text(store, tenant, document, page.text))
    return document.model_dump(mode="json")


@router.post("/tenants/{tenant_id}/knowledge/{document_id}/delete")
async def delete_knowledge_document(
    tenant_id: str,
    document_id: str,
    principal: AdminPrincipal = Depends(require_tenant_access),
    _enabled: None = Depends(require_knowledge_enabled),
) -> dict:
    await get_store().adelete_document(tenant_id, document_id)
    return {"deleted": document_id}


@router.post("/tenants/{tenant_id}/knowledge/search")
async def search_knowledge_preview(
    tenant_id: str,
    payload: KnowledgeSearchRequest,
    principal: AdminPrincipal = Depends(require_tenant_access),
    _enabled: None = Depends(require_knowledge_enabled),
) -> dict:
    """What the bot would actually retrieve for this question, before it
    goes live — the same `search_chunks` call `app/tools/knowledge_tools.py`
    makes at conversation time, using this tenant's own `top_k`/
    `min_similarity` (`TenantConfig.knowledge`)."""
    from app.rag.embeddings import EmbeddingError, embed_text

    tenant = get_tenant_config(tenant_id)
    try:
        query_embedding = await embed_text(payload.query)
    except EmbeddingError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    if not query_embedding:
        return {"hits": []}

    hits = await get_store().asearch_chunks(
        tenant_id,
        query_embedding=query_embedding,
        top_k=tenant.knowledge.top_k,
        min_similarity=tenant.knowledge.min_similarity,
    )
    return {"hits": [h.model_dump(mode="json") for h in hits]}


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
