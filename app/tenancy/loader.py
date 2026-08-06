"""Per-tenant config loader with a short TTL cache.

Plan §6c: one DB read per cold conversation, not one per turn. Every node and
every tool goes through `get_tenant_config`, so the cache is the only place
that ever touches the repository.
"""

from __future__ import annotations

import logging
import time
from threading import RLock

from langchain_core.runnables import RunnableConfig

from app.config import get_settings
from app.tenancy.models import TenantConfig
from app.tenancy.repository import (
    ChannelDisabledError,
    JsonFileTenantRepository,
    TenantArchivedError,
    TenantNotFoundError,
    TenantRepository,
)

logger = logging.getLogger(__name__)

_lock = RLock()
_repository: TenantRepository | None = None
_cache: dict[str, tuple[float, TenantConfig]] = {}


def get_repository() -> TenantRepository:
    global _repository
    with _lock:
        if _repository is None:
            _repository = JsonFileTenantRepository(get_settings().tenant_data_dir)
        return _repository


def set_repository(repository: TenantRepository | None) -> None:
    """Swap the backing store (Phase 4 Supabase impl, or a test double)."""
    global _repository
    with _lock:
        _repository = repository
        _cache.clear()


def clear_tenant_cache() -> None:
    with _lock:
        _cache.clear()


async def refresh_tenant_repository() -> None:
    """Refresh whatever's currently serving reads, after any write that
    changed Supabase. Duck-typed: json mode's `JsonFileTenantRepository` has
    neither method, so this is a no-op there — exactly right, since
    `content/tenants/*.json` wasn't the thing that changed. Shared by every
    Supabase write across the codebase (`app/tenancy/admin.py`'s
    `save_tenant`/`create_tenant`/`set_tenant_status`/`purge_tenant`, and
    `app/channels/vapi_provisioning.py`'s Supabase-only fallback) — without
    it, a change doesn't appear until the 300s background refresh, which is
    exactly the "phantom edit" class of bug plans/phase8.md documents.
    """
    clear_tenant_cache()
    repository = get_repository()
    refresh = getattr(repository, "refresh", None)
    if refresh is not None:
        await refresh()
    else:
        invalidate = getattr(repository, "invalidate", None)
        if invalidate is not None:
            invalidate()


def get_tenant_config(tenant_id: str) -> TenantConfig:
    """Load a tenant profile, memoised for `TENANT_CACHE_TTL_SECONDS`."""
    ttl = get_settings().tenant_cache_ttl_seconds
    now = time.monotonic()

    with _lock:
        cached = _cache.get(tenant_id)
        if cached and now - cached[0] < ttl:
            return cached[1]

    config = get_repository().get(tenant_id)

    with _lock:
        _cache[tenant_id] = (now, config)
    return config


def tenant_config_from_runnable(tenant_id: str, config: RunnableConfig | None) -> TenantConfig:
    """The config a graph node/tool should reason with for THIS turn —
    almost always `get_tenant_config(tenant_id)` (the shared, live cache),
    except inside a draft-preview conversation (Test Agent's "Preview
    draft" link), where `app/brain/runner.py::stream_turn` has already
    resolved the tenant's current draft and stashed it in
    `RunnableConfig.configurable["tenant_config_override"]` for this one
    thread only.

    Never the shared cache for a draft: that cache is keyed by `tenant_id`
    alone and read by every real caller of this tenant, so writing a draft
    into it would leak an unpublished, unreviewed edit onto a live phone
    call or widget visitor. The override travels through `configurable`
    instead — supplied fresh by `stream_turn` on every turn, never
    persisted by the checkpointer, never reachable from a model argument or
    request body.

    The one function `app/brain/nodes/resolve_tenant.py`, `reason.py`,
    `tools.py` and `app/tools/context.py::tenant_from_config` all call —
    same "both bind sites can't independently drift" reasoning Phase 6/9
    Part C already established for MCP/knowledge tool binding.
    """
    configurable = (config or {}).get("configurable") or {}
    override = configurable.get("tenant_config_override")
    if override is not None:
        return override
    return get_tenant_config(tenant_id)


def _refuse_if_archived(config: TenantConfig) -> None:
    """Every successful resolution below goes through this — a soft-deleted
    (`status: "archived"`, Phase 9 Part B) bot must not answer on any
    channel, no matter how it was addressed (explicit id, assistant id,
    dialled number, or widget key)."""
    if config.status == "archived":
        logger.warning(
            "refusing archived tenant %s — resolved but not serving it", config.tenant_id
        )
        raise TenantArchivedError(f"tenant {config.tenant_id!r} is archived")


def resolve_tenant_id(
    *,
    tenant_id: str | None = None,
    assistant_id: str | None = None,
    phone_number: str | None = None,
    widget_key: str | None = None,
) -> str:
    """Map an inbound identifier to a tenant_id (plan §6a).

    Voice hands us the Vapi assistant id and the dialled number, chat hands us a
    widget key, and the CLI hands us an explicit id. Assistant id wins because
    it is the most specific: one assistant belongs to exactly one tenant, while
    a number could in principle be re-pointed.

    Anything unresolved falls back to the configured default tenant, so a
    single-tenant deployment needs no special-casing.
    """
    if tenant_id:
        config = get_tenant_config(tenant_id)  # validates existence
        _refuse_if_archived(config)
        return tenant_id

    repository = get_repository()
    if assistant_id:
        config = repository.find_by_assistant_id(assistant_id)
        if config:
            _refuse_if_archived(config)
            return config.tenant_id
    if phone_number:
        config = repository.find_by_phone(phone_number)
        if config:
            _refuse_if_archived(config)
            return config.tenant_id
    if widget_key:
        config = repository.find_by_widget_key(widget_key)
        if config:
            _refuse_if_archived(config)
            return config.tenant_id

    # An unknown assistant id is not fatal on its own: a freshly provisioned
    # assistant may not be written back to tenant config yet, and the dialled
    # number is a perfectly good fallback. Only give up when nothing matched.
    if phone_number or widget_key:
        raise TenantNotFoundError(
            f"no tenant matches assistant={assistant_id!r} phone={phone_number!r} "
            f"widget_key={widget_key!r}"
        )

    default_id = get_settings().default_tenant_id
    config = get_tenant_config(default_id)
    _refuse_if_archived(config)
    return default_id


def require_channel_enabled(config: TenantConfig, channel: str) -> None:
    """Phase 9.1 — `channels.chat.enabled` / `channels.voice.enabled`.

    Not folded into `resolve_tenant_id` (unlike `TenantArchivedError`'s
    check): that function has no `channel` argument, since callers resolve
    a tenant before they necessarily know which channel it's for. Called
    explicitly instead, once a channel is known: `app/channels/chat.py`'s
    two endpoints, `app/channels/vapi_llm.py`, and `/test/session`
    (`app/main.py`). Defaults are true/true, so this is a no-op for every
    tenant that hasn't opted out of a channel.
    """
    toggle = {"chat": config.channels.chat, "voice": config.channels.voice}.get(channel)
    if toggle is not None and not toggle.enabled:
        logger.warning(
            "refusing tenant %s on channel=%s — channel disabled", config.tenant_id, channel
        )
        raise ChannelDisabledError(f"tenant {config.tenant_id!r} has channel {channel!r} disabled")
