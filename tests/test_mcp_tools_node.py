"""app/brain/nodes/tools.py — the dynamic tools node (Phase 6).

The regression this file exists to guard: before Phase 6, `app/brain/graph.py`
built `ToolNode(NATIVE_TOOLS)` once, at compile time, from a static list,
while `reason` bound a per-tenant set (native + MCP) on every turn. A model
could then emit a call for an MCP tool the static `tools` node had never
heard of. These tests prove the dynamic node actually executes what `reason`
bound — not just that MCP tools exist in isolation (that's
test_mcp_loader.py's job).
"""

from __future__ import annotations

import importlib

from langchain_core.messages import ToolMessage

from app.brain.runner import stream_turn
from tests._mcp_fakes import fake_tool
from tests.conftest import ai

# app/brain/nodes/__init__.py does `from .reason import reason`, which
# rebinds the `nodes` package's `reason` *attribute* to the function —
# shadowing the submodule for any attribute-chain lookup, including
# `import app.brain.nodes.reason as x` (that statement also resolves via
# attribute access on the parent package, not via sys.modules). Only
# `importlib.import_module`, which returns the sys.modules entry directly,
# reliably gets the real submodule object to patch.
_reason_module = importlib.import_module("app.brain.nodes.reason")
_tools_module = importlib.import_module("app.brain.nodes.tools")


def _last_tool_message(messages) -> ToolMessage:
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            return message
    raise AssertionError("no ToolMessage in the conversation")


async def _install_fake_mcp(monkeypatch, tools):
    async def _fake_load(tenant, *, client=None):
        return list(tools)

    # Both nodes did `from app.mcp.client import load_mcp_tools` at import
    # time, so each holds its own local binding — patching the source
    # module's attribute alone would not reach either call site.
    monkeypatch.setattr(_reason_module, "load_mcp_tools", _fake_load)
    monkeypatch.setattr(_tools_module, "load_mcp_tools", _fake_load)


async def test_an_mcp_tool_bound_in_reason_is_executed_by_the_tools_node(
    scripted, hotel, monkeypatch
):
    await _install_fake_mcp(monkeypatch, [fake_tool("lookup_guest_loyalty")])

    model = scripted(
        ai(
            "Let me check that. ",
            [{"name": "lookup_guest_loyalty", "args": {}}],
        ),
        lambda messages: ai(f"Found it: {_last_tool_message(messages).content}"),
    )

    events = [
        event
        async for event in stream_turn(
            text="what's my loyalty status?", tenant_id=hotel.tenant_id, session_id="mcp-node-1"
        )
    ]

    assert "lookup_guest_loyalty" in model.bound_tool_names
    final = next(e for e in events if e.type == "final")
    assert "lookup_guest_loyalty ran" in final.text
    assert not any(e.type == "error" for e in events)


async def test_native_tools_still_execute_with_mcp_tools_bound(scripted, hotel, monkeypatch):
    await _install_fake_mcp(monkeypatch, [fake_tool("some_mcp_tool")])

    scripted(
        ai(
            "Checking now. ",
            [{"name": "check_availability", "args": {"service": "room-reservation"}}],
        ),
        ai("Got a few slots for you."),
    )

    events = [
        event
        async for event in stream_turn(
            text="room tonight?", tenant_id=hotel.tenant_id, session_id="mcp-node-2"
        )
    ]

    assert not any(e.type == "error" for e in events)
    final = next(e for e in events if e.type == "final")
    assert final.text


async def test_a_tenant_with_no_mcp_servers_behaves_as_before(scripted, hotel, monkeypatch):
    """MCP enabled, but this tenant has none configured — the dynamic node's
    resolved tool set must reduce to exactly the native five, same as a
    tenant that never heard of MCP."""
    await _install_fake_mcp(monkeypatch, [])  # empty: no servers for this tenant

    scripted(
        ai(
            "Checking now. ",
            [{"name": "check_availability", "args": {"service": "room-reservation"}}],
        ),
        ai("Got a few slots for you."),
    )

    events = [
        event
        async for event in stream_turn(
            text="room tonight?", tenant_id=hotel.tenant_id, session_id="mcp-node-3"
        )
    ]

    assert not any(e.type == "error" for e in events)
    final = next(e for e in events if e.type == "final")
    assert final.text
