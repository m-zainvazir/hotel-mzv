"""Node 4 — execute whatever tool `reason` called (Phase 6).

Before Phase 6 this was a plain `ToolNode(NATIVE_TOOLS)`, built once at graph
compile time from a static list. That was silently wrong the moment MCP tools
existed: `reason` binds `native_tools_for(tenant, channel) +
await load_mcp_tools(tenant)` **per tenant, per turn**
(`app/brain/nodes/reason.py`), so the model can call an MCP tool the static
`ToolNode` has never heard of. The call would fail with no clue why in the
logs.

This node resolves the same tool set `reason` just bound and builds a fresh
`ToolNode` from it. Two things make that cheap rather than wasteful:
`ToolNode(...)` construction is just name-indexing, no I/O; and
`load_mcp_tools` is a cache hit here in the overwhelmingly common case,
because `reason` already populated it earlier in the same turn
(`app/mcp/client.py`'s cache is keyed to make bind and execute provably the
same set — see its docstring for why that's a correctness requirement, not
just a latency one).
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import ToolNode

from app.brain.state import ReceptionistState
from app.mcp.client import load_mcp_tools
from app.tenancy.loader import get_tenant_config
from app.tools.registry import native_tools_for


async def tools(state: ReceptionistState, config: RunnableConfig) -> dict:
    tenant = get_tenant_config(state["tenant_id"])
    channel = state.get("channel", "chat")

    available = native_tools_for(tenant, channel) + await load_mcp_tools(tenant)
    return await ToolNode(available).ainvoke(state, config)
