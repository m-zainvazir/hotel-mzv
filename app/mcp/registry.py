"""Which MCP servers a tenant may use (Phase 6).

Two read paths behind `settings.mcp_source`, same shape as `TENANT_SOURCE` —
but safe to flip to `"supabase"` in production without a live-verification
pass of its own. Unlike the full tenant-config read path, `servers_for()` is
only ever called from inside `app/mcp/client.py::load_mcp_tools`, which is
already async, already gated on `settings.mcp_enabled` (off by default), and
already degrades to `[]` on any failure. No autouse test fixture walks this
at collection time the way `isolated_runtime` walks
`get_repository().list_ids()` for tenant config — that's the reason the full
flip stays deferred (`plans/phase10.md` item 8) while this one doesn't.

**Any failure here returns `[]` with a WARNING — it never raises.** This is
deliberately the opposite posture to `app/tenancy/secrets.py`, where an
errored vault lookup must NOT be treated as absent (that would leak a tenant
into another tenant's real calendar). A missing MCP tool is a smaller
capability, not a wrong action, so a registry outage degrades to "no
long-tail tools this turn" rather than costing a booking.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.db.auth import SupabaseAuthNotConfiguredError, tenant_jwt
from app.tenancy.models import McpServerConfig, TenantConfig

logger = logging.getLogger(__name__)


async def servers_for(
    tenant: TenantConfig, *, client: httpx.AsyncClient | None = None
) -> list[McpServerConfig]:
    """The tenant's *enabled* MCP servers, from whichever source is configured."""
    settings = get_settings()
    if settings.mcp_source == "json":
        return [server for server in tenant.mcp_servers if server.enabled]
    return await _from_supabase(tenant.tenant_id, client=client)


async def _from_supabase(
    tenant_id: str, *, client: httpx.AsyncClient | None = None
) -> list[McpServerConfig]:
    settings = get_settings()
    if not settings.supabase_url:
        # Consistent with every other Supabase-backed reader: no project
        # configured means "nothing to read", not an error.
        return []

    try:
        headers = {"Authorization": f"Bearer {tenant_jwt(tenant_id)}"}
    except SupabaseAuthNotConfiguredError:
        logger.warning(
            "mcp_source=supabase but SUPABASE_JWT_SECRET is unset — cannot mint a "
            "tenant-scoped token, returning no MCP servers for %s",
            tenant_id,
        )
        return []

    request_client = client or _shared_client(settings)
    try:
        # Explicit tenant_id filter even though RLS also enforces it —
        # CLAUDE.md convention #3: the application layer must never rely on
        # RLS alone.
        response = await request_client.get(
            "/mcp_servers",
            params={"tenant_id": f"eq.{tenant_id}", "enabled": "is.true"},
            headers=headers,
        )
    except httpx.HTTPError as exc:
        logger.warning("mcp registry request failed for %s: %s", tenant_id, exc)
        return []

    if response.status_code >= 400:
        logger.warning(
            "mcp registry request for %s -> %d: %s",
            tenant_id,
            response.status_code,
            response.text[:200],
        )
        return []

    if not response.content:
        return []

    rows = response.json()
    if not isinstance(rows, list):
        return []

    servers: list[McpServerConfig] = []
    for row in rows:
        try:
            servers.append(_row_to_config(row))
        except Exception:
            logger.warning(
                "mcp_servers row for %s/%s is malformed — skipping",
                tenant_id,
                row.get("name", "?"),
                exc_info=True,
            )
    return servers


def _row_to_config(row: dict) -> McpServerConfig:
    return McpServerConfig(
        name=row["name"],
        enabled=row.get("enabled", True),
        transport=row.get("transport", "http"),
        url=row.get("url"),
        headers=row.get("headers") or {},
        tool_allowlist=row.get("tool_allowlist") or [],
        command=row.get("command"),
        args=row.get("args") or [],
        auth_secret_ref=row.get("auth_secret_ref"),
    )


def _shared_client(settings) -> httpx.AsyncClient:
    # Local import — same reasoning as app/tenancy/secrets.py: app.tools.http_client
    # sits under app.tools, whose __init__ eagerly imports the native tool
    # registry, which can reach back into app.tenancy / app.mcp.
    from app.tools.http_client import shared_async_client

    key = f"supabase-rest:{settings.supabase_url}"
    return shared_async_client(
        key,
        base_url=f"{settings.supabase_url}/rest/v1",
        headers={"apikey": settings.supabase_anon_key or ""},
        timeout=settings.supabase_timeout_seconds,
    )
