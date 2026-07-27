"""Push tenant JSON config into Supabase `tenants`/`services` (Phase 4).

The tenant *read* path stays on `content/tenants/*.json` for Phase 4 (see the
plan's "On the tenant read path" note) — these tables exist so onboarding has
somewhere durable to write to, and so the eventual read-path flip
(`TENANT_SOURCE=supabase`) has real data waiting. Nothing reads them today.

Writes go through the Supabase **secret** key (admin path, bypasses RLS):
pushing a tenant's config isn't a "this tenant's own data" request the way a
booking is, it's a cross-tenant backend-operator action — the same reasoning
`app/tenancy/secrets.py::set_tenant_secret` uses.

Shared by `scripts/onboard_tenant.py` and `scripts/sync_tenants.py` so the
two scripts can't drift into writing the row shape differently.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings
from app.tenancy.models import TenantConfig

# Columns that exist on `tenants` in their own right — everything else on
# TenantConfig (greeting, persona, hours, emergency, booking, notifications,
# voice, vapi, mcp_servers) goes into the `config` JSONB blob, since nothing
# reads them relationally yet (see app/db/migrations/0001_schema.sql).
_TENANT_COLUMNS = {
    "tenant_id",
    "name",
    "trade",
    "timezone",
    "status",
    "phone_numbers",
    "widget_keys",
    "services",
}


class TenantSyncError(RuntimeError):
    pass


def _admin_client(settings, *, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{settings.supabase_url}/rest/v1",
        headers={
            "apikey": settings.supabase_secret_key or "",
            "Authorization": f"Bearer {settings.supabase_secret_key}",
            "Prefer": "resolution=merge-duplicates,return=representation",
        },
        timeout=timeout,
    )


def _tenant_row(tenant: TenantConfig) -> dict[str, Any]:
    config = tenant.model_dump(mode="json", exclude=_TENANT_COLUMNS)
    return {
        "tenant_id": tenant.tenant_id,
        "name": tenant.name,
        "trade": tenant.trade,
        "timezone": tenant.timezone,
        "status": tenant.status,
        "phone_numbers": tenant.phone_numbers,
        "widget_keys": tenant.widget_keys,
        "config": config,
    }


def _service_rows(tenant: TenantConfig) -> list[dict[str, Any]]:
    return [
        {
            "tenant_id": tenant.tenant_id,
            "slug": s.slug,
            "name": s.name,
            "description": s.description,
            "duration_minutes": s.duration_minutes,
            "price_usd": s.price_usd,
            "emergency": s.emergency,
            "aliases": s.aliases,
            "event_type_id": s.event_type_id,
        }
        for s in tenant.services
    ]


def _mcp_server_rows(tenant: TenantConfig) -> list[dict[str, Any]]:
    """Phase 6: mirrors `_service_rows` so a JSON-edited server list and the
    `mcp_servers` table always agree, and `MCP_SOURCE` can be flipped either
    direction without surprise. Note `headers` here may still contain the
    literal `${secret}` placeholder — the resolved value only ever exists in
    Vault, via `set_tenant_secret` (see `scripts/register_mcp_server.py`)."""
    return [
        {
            "tenant_id": tenant.tenant_id,
            "name": s.name,
            "enabled": s.enabled,
            "transport": s.transport,
            "url": s.url,
            "headers": s.headers,
            "tool_allowlist": s.tool_allowlist,
            "command": s.command,
            "args": s.args,
            "auth_secret_ref": s.auth_secret_ref,
        }
        for s in tenant.mcp_servers
    ]


async def sync_tenant(tenant: TenantConfig, *, client: httpx.AsyncClient | None = None) -> None:
    """Upsert this tenant's `tenants` row and every `services` row."""
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_secret_key:
        raise TenantSyncError(
            "SUPABASE_URL / SUPABASE_SECRET_KEY must be set to sync a tenant to Supabase"
        )

    owns_client = client is None
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
    try:
        tenant_response = await active.post(
            "/tenants", params={"on_conflict": "tenant_id"}, json=_tenant_row(tenant)
        )
        if tenant_response.status_code >= 400:
            raise TenantSyncError(
                f"could not sync tenant row: {tenant_response.status_code} "
                f"{tenant_response.text[:300]}"
            )

        rows = _service_rows(tenant)
        if rows:
            services_response = await active.post(
                "/services", params={"on_conflict": "tenant_id,slug"}, json=rows
            )
            if services_response.status_code >= 400:
                raise TenantSyncError(
                    f"could not sync services: {services_response.status_code} "
                    f"{services_response.text[:300]}"
                )

        mcp_rows = _mcp_server_rows(tenant)
        if mcp_rows:
            mcp_response = await active.post(
                "/mcp_servers", params={"on_conflict": "tenant_id,name"}, json=mcp_rows
            )
            if mcp_response.status_code >= 400:
                raise TenantSyncError(
                    f"could not sync mcp_servers: {mcp_response.status_code} "
                    f"{mcp_response.text[:300]}"
                )
    finally:
        if owns_client:
            await active.aclose()
