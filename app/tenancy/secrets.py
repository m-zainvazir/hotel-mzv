"""Per-tenant secrets: Vault first, global env fallback (Phase 4 Step 6).

This is what unblocks per-tenant Cal.com/Twilio credentials — today one
shared `CALCOM_API_KEY` is `hotel-mzv`'s own account, and any other tenant
flipped to `"calcom"` books into *hotel-mzv's real calendar*, isolated only
by `event_type_id`.

Same cache shape as `app/tenancy/loader.py` (`RLock` + `dict[key, (ts, val)]`
+ TTL + `clear_*_cache()`) — this can't live inside a provider, because
`app/tools/providers.py` constructs a fresh provider on every tool call.

The one rule that matters here: **absent is not the same as errored.**
"""

from __future__ import annotations

import time
from threading import RLock

import httpx

from app.config import get_settings
from app.db.auth import tenant_jwt


class TenantSecretError(RuntimeError):
    """The vault could not be reached or returned an error.

    Deliberately distinct from "the tenant has no secret under this name"
    (which is a legitimate `None`, handled by `resolve_secret`'s env
    fallback). This must NEVER be treated as "absent": today's global env
    credentials (e.g. `CALCOM_API_KEY`) are one tenant's real account, so
    silently falling back on a vault timeout would book a *different*
    tenant into that account's real calendar — a cross-tenant leak, not a
    convenience.
    """


_lock = RLock()
_cache: dict[tuple[str, str], tuple[float, str | None]] = {}


async def get_tenant_secret(
    tenant_id: str, key_name: str, *, client: httpx.AsyncClient | None = None
) -> str | None:
    """The tenant's own secret from Vault, or `None` if it genuinely has none.

    Cached briefly (`secret_cache_ttl_seconds`). On a vault error, a cached
    value — even a stale one — is served rather than taking booking offline
    over a transient Supabase blip; only when nothing is cached at all does
    `TenantSecretError` propagate.
    """
    settings = get_settings()
    cache_key = (tenant_id, key_name)
    now = time.monotonic()

    with _lock:
        cached = _cache.get(cache_key)
    if cached is not None and now - cached[0] < settings.secret_cache_ttl_seconds:
        return cached[1]

    try:
        value = await _fetch(tenant_id, key_name, client=client)
    except TenantSecretError:
        if cached is not None:
            return cached[1]
        raise

    with _lock:
        _cache[cache_key] = (now, value)
    return value


async def resolve_secret(
    tenant_id: str,
    key_name: str,
    env_value: str | None,
    *,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Per-tenant Vault secret, falling back to a global env value only when
    the tenant genuinely has none configured. A vault error (timeout, 5xx,
    401) raises `TenantSecretError` instead — see that class's docstring for
    why falling back there would be a cross-tenant credential leak.
    """
    value = await get_tenant_secret(tenant_id, key_name, client=client)
    return value if value is not None else env_value


async def _fetch(tenant_id: str, key_name: str, *, client: httpx.AsyncClient | None) -> str | None:
    settings = get_settings()
    if not settings.supabase_url:
        # No vault configured at all — every tenant is "absent", not errored.
        return None

    request_client = client or _shared_client()
    try:
        response = await request_client.post(
            "/rpc/get_tenant_secret",
            headers={"Authorization": f"Bearer {tenant_jwt(tenant_id)}"},
            json={"key_name": key_name},
        )
    except httpx.TimeoutException as exc:
        raise TenantSecretError(f"vault request timed out for {tenant_id}/{key_name}") from exc
    except httpx.HTTPError as exc:
        raise TenantSecretError(f"vault request failed for {tenant_id}/{key_name}: {exc}") from exc

    if response.status_code >= 400:
        raise TenantSecretError(
            f"vault RPC {tenant_id}/{key_name} -> {response.status_code}: {response.text[:200]}"
        )
    if not response.content:
        return None
    data = response.json()
    return data if isinstance(data, str) else None


def _shared_client() -> httpx.AsyncClient:
    # Local import — see the matching note in app/db/supabase_store.py:
    # app.tools.http_client sits under the app.tools package, whose __init__
    # eagerly imports the native tool registry, which can reach back here.
    from app.tools.http_client import shared_async_client

    settings = get_settings()
    key = f"supabase-rest:{settings.supabase_url}"
    return shared_async_client(
        key,
        base_url=f"{settings.supabase_url}/rest/v1",
        headers={"apikey": settings.supabase_anon_key or ""},
        timeout=settings.supabase_timeout_seconds,
    )


async def set_tenant_secret(
    tenant_id: str,
    key_name: str,
    secret_value: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Write a per-tenant secret into Vault (Phase 4 Step 9 — onboarding).

    Goes through the Supabase **secret** key (admin path, bypasses RLS) via
    `public.set_tenant_secret`'s RPC — never the tenant-scoped JWT
    `get_tenant_secret` above uses, since writing a secret is a
    backend-operator action, not a "this tenant reading its own data" one.
    `set_tenant_secret`'s SQL is deliberately NOT granted to `app_backend`.
    """
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_secret_key:
        raise TenantSecretError(
            "SUPABASE_URL / SUPABASE_SECRET_KEY must be set to write a tenant secret"
        )

    owns_client = client is None
    active = client or _admin_client(settings)
    try:
        response = await active.post(
            "/rpc/set_tenant_secret",
            json={"tenant_id": tenant_id, "key_name": key_name, "secret_value": secret_value},
        )
    except httpx.HTTPError as exc:
        raise TenantSecretError(f"could not write secret {tenant_id}/{key_name}: {exc}") from exc
    finally:
        if owns_client:
            await active.aclose()

    if response.status_code >= 400:
        raise TenantSecretError(
            f"set_tenant_secret {tenant_id}/{key_name} -> {response.status_code}: "
            f"{response.text[:200]}"
        )


def _admin_client(settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{settings.supabase_url}/rest/v1",
        headers={
            "apikey": settings.supabase_secret_key or "",
            "Authorization": f"Bearer {settings.supabase_secret_key}",
        },
        timeout=settings.supabase_timeout_seconds,
    )


def clear_secret_cache() -> None:
    """Test hook — drop cached secrets so a swapped vault value takes effect."""
    with _lock:
        _cache.clear()
