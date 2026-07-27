"""MCP tool loading — tier 2 (Phase 6).

Keeps the same signature `app/brain/nodes/reason.py` has called since Phase 1
(`async def load_mcp_tools(tenant) -> list[BaseTool]`), so that module is
untouched by this rewrite. Gains `*, client=None` purely for test injection,
matching `CalcomBookingProvider` / `TwilioNotifier` / `SupabaseStore`.

Three postures worth stating up front, because they're each the opposite of
what a naive implementation would do:

* **A missing `langchain-mcp-adapters` install degrades to `[]`, never
  raises.** The package is an optional extra (`pip install -e ".[mcp]"`)
  precisely because its own dependency chain pulls three compiled packages
  (`rpds-py`, `cryptography`, `pywin32` on win32) — see CLAUDE.md's
  `uuid_utils` gotcha for why a blocked compiled DLL on this box once broke
  every import in the project, not just its own.
* **One dead or slow server never blocks the others, or the turn.** Each
  server is connected to independently, wrapped in its own
  `asyncio.wait_for`. A third-party MCP server is not something we control.
* **The cache is a correctness requirement, not just a latency one.**
  `MultiServerMCPClient` is stateless — every `get_tools()` opens fresh
  sessions — so without a cache, `reason` would pay a full MCP handshake on
  *every* LLM request, including each tool hop. Worse: `reason` binds this
  list and the dynamic `tools` node (`app/brain/nodes/tools.py`) executes
  against it in the same turn. If the two calls disagreed — a server that
  flaked between them — the model could emit a call nothing can run. One
  cached list per tenant per TTL makes bind and execute provably the same
  set.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from threading import RLock
from typing import Any

from langchain_core.tools import BaseTool

from app.config import get_settings
from app.mcp.connections import build_connection, redacted
from app.mcp.registry import servers_for
from app.tenancy.models import TenantConfig

logger = logging.getLogger(__name__)

_lock = RLock()
#: tenant_id -> (cached_at, connection_fingerprint, tools)
_cache: dict[str, tuple[float, str, list[BaseTool]]] = {}


async def load_mcp_tools(tenant: TenantConfig, *, client: Any = None) -> list[BaseTool]:
    settings = get_settings()
    if not settings.mcp_enabled:
        return []

    servers = await servers_for(tenant, client=client)
    if not servers:
        return []

    connections = await _build_connections(tenant.tenant_id, servers)
    if not connections:
        return []

    fingerprint = _fingerprint(connections)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(tenant.tenant_id)
        if (
            cached
            and cached[1] == fingerprint
            and now - cached[0] < settings.mcp_tool_cache_ttl_seconds
        ):
            return cached[2]

    tools = await _load_fresh(tenant.tenant_id, servers, connections)

    with _lock:
        _cache[tenant.tenant_id] = (now, fingerprint, tools)
    return tools


async def _build_connections(tenant_id: str, servers: list) -> dict[str, dict[str, Any]]:
    connections: dict[str, dict[str, Any]] = {}
    for server in servers:
        connection = await build_connection(tenant_id, server)
        if connection is not None:  # build_connection already warned on skip
            connections[server.name] = connection
    return connections


async def _load_fresh(
    tenant_id: str, servers: list, connections: dict[str, dict[str, Any]]
) -> list[BaseTool]:
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.warning(
            "MCP_ENABLED is true but langchain-mcp-adapters is not installed — "
            'run `pip install -e ".[mcp]"`. Returning no MCP tools for %s.',
            tenant_id,
        )
        return []

    settings = get_settings()
    # tool_name_prefix=True is plan §11's collision safety, opt-in as of
    # adapter 0.3.0: without it, two servers exposing a "search" tool would
    # silently shadow each other. Confirmed (adapter source, tools.py) the
    # separator is an underscore: f"{server_name}_{tool.name}" — combined
    # with the ^[a-z0-9][a-z0-9_-]{0,31}$ name validator on McpServerConfig,
    # a prefixed name always satisfies providers' ^[a-zA-Z0-9_-]{1,64}$ rule.
    mcp_client = MultiServerMCPClient(connections, tool_name_prefix=True)

    by_name = {server.name: server for server in servers}
    tools: list[BaseTool] = []
    for server_name in connections:
        try:
            server_tools = await asyncio.wait_for(
                mcp_client.get_tools(server_name=server_name),
                timeout=settings.mcp_connect_timeout_seconds,
            )
        except Exception:
            logger.warning(
                "mcp server %s/%s could not be loaded — skipping it this turn (connection: %r)",
                tenant_id,
                server_name,
                redacted(connections[server_name]),
                exc_info=True,
            )
            continue

        allowlist = by_name[server_name].tool_allowlist
        if allowlist:
            prefix = f"{server_name}_"
            server_tools = [
                tool for tool in server_tools if tool.name.removeprefix(prefix) in allowlist
            ]
        tools.extend(server_tools)

    if len(tools) > settings.mcp_max_tools:
        dropped = [tool.name for tool in tools[settings.mcp_max_tools :]]
        logger.warning(
            "tenant %s resolved %d MCP tools, truncating to MCP_MAX_TOOLS=%d — "
            "dropped %s. Narrow this tenant's server list or tool_allowlist.",
            tenant_id,
            len(tools),
            settings.mcp_max_tools,
            dropped,
        )
        tools = tools[: settings.mcp_max_tools]

    return tools


def _fingerprint(connections: dict[str, dict[str, Any]]) -> str:
    """Changes whenever the resolved server set or a resolved secret changes,
    so a rotated token or an edited server list invalidates the cache
    immediately rather than waiting out the TTL."""
    payload = json.dumps(connections, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def clear_mcp_cache() -> None:
    """Test hook — drop cached MCP tool lists so config changes take effect."""
    with _lock:
        _cache.clear()
