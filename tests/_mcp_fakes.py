"""Shared fake `MultiServerMCPClient` for MCP tests (Phase 6).

Not a test module itself — doesn't match pytest's `test_*.py` collection
pattern — just scaffolding shared by test_mcp_loader.py and
test_tenant_isolation.py so neither duplicates it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.tools import StructuredTool


def fake_tool(name: str) -> StructuredTool:
    async def _run() -> str:
        return f"{name} ran"

    return StructuredTool.from_function(coroutine=_run, name=name, description="a fake mcp tool")


def make_fake_client(
    *,
    server_tools: dict[str, list[str]] | None = None,
    hang: tuple[str, ...] = (),
    raise_for: tuple[str, ...] = (),
) -> tuple[type, dict[str, int]]:
    """A `MultiServerMCPClient` stand-in plus a call counter.

    `server_tools` maps server_name -> bare tool names to return.
    `hang` / `raise_for` name servers whose `get_tools()` should sleep past
    any caller-side timeout, or raise — so tests can prove one bad server
    never blocks or kills the others.

    `calls["constructed"]` counts how many times the class was
    instantiated — exactly once per `app.mcp.client._load_fresh` call, so
    it's a direct proxy for "was the tenant's cache actually hit".
    """
    tools_by_server = server_tools or {}
    hanging = set(hang)
    erroring = set(raise_for)
    calls = {"constructed": 0}

    class _FakeMultiServerMCPClient:
        def __init__(
            self,
            connections: dict[str, Any],
            *,
            tool_name_prefix: bool = False,
            handle_tool_errors: bool = True,
        ) -> None:
            self.connections = connections
            self.tool_name_prefix = tool_name_prefix
            calls["constructed"] += 1

        async def get_tools(self, *, server_name: str | None = None) -> list[StructuredTool]:
            if server_name in erroring:
                raise RuntimeError(f"{server_name} exploded")
            if server_name in hanging:
                await asyncio.sleep(30)
            names = tools_by_server.get(server_name, ["search"])
            prefix = f"{server_name}_" if self.tool_name_prefix else ""
            return [fake_tool(f"{prefix}{name}") for name in names]

    return _FakeMultiServerMCPClient, calls
