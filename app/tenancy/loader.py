"""Per-tenant config loader with a short TTL cache.

Plan §6c: one DB read per cold conversation, not one per turn. Every node and
every tool goes through `get_tenant_config`, so the cache is the only place
that ever touches the repository.
"""

from __future__ import annotations

import time
from threading import RLock

from app.config import get_settings
from app.tenancy.models import TenantConfig
from app.tenancy.repository import (
    JsonFileTenantRepository,
    TenantNotFoundError,
    TenantRepository,
)

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
        get_tenant_config(tenant_id)  # validates existence
        return tenant_id

    repository = get_repository()
    if assistant_id:
        config = repository.find_by_assistant_id(assistant_id)
        if config:
            return config.tenant_id
    if phone_number:
        config = repository.find_by_phone(phone_number)
        if config:
            return config.tenant_id
    if widget_key:
        config = repository.find_by_widget_key(widget_key)
        if config:
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
    get_tenant_config(default_id)
    return default_id
