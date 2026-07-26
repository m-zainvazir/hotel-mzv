"""MCP tool loading — tier 2 (Phase 6).

Phase 1 returns an empty list, but the call site exists in the reason node so
that turning MCP on later is a config change, not a graph change. When this is
implemented it must:

  * build a `MultiServerMCPClient` from the tenant's `mcp_servers` rows only —
    never a global server list (CLAUDE.md convention #3);
  * resolve auth from the encrypted secrets store by reference;
  * prefix tool names by server to avoid collisions;
  * apply `MCP_TOOL_TIMEOUT_SECONDS` so a slow server can't blow the latency
    budget (plan §11).
"""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool

from app.config import get_settings
from app.tenancy.models import TenantConfig

logger = logging.getLogger(__name__)


async def load_mcp_tools(tenant: TenantConfig) -> list[BaseTool]:
    settings = get_settings()
    if not settings.mcp_enabled or not tenant.mcp_servers:
        return []

    logger.warning(
        "MCP is enabled and tenant %s has %d server(s) configured, but the MCP layer "
        "lands in Phase 6 — returning no tools.",
        tenant.tenant_id,
        len(tenant.mcp_servers),
    )
    return []
