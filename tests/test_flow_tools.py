"""start_flow (Phase 9.2) — the model's way *into* a deterministic flow.

The assertion that matters most is `TestGraphTermination`: a flow node must
end the turn as a *graph edge*, not because the prompt asked the model
nicely. Everything else here is conditional binding and error recovery.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.brain.runner import stream_turn
from app.main import app
from app.tenancy.models import FlowNode, TenantLink
from app.tools.flow_tools import start_flow
from app.tools.registry import SLOW_TOOLS, native_tools_for
from tests.conftest import ai, tool_config

_LINKS = [
    TenantLink(slug="browse", label="📍 Browse Locations", url="https://ppt.example.com/find"),
    TenantLink(slug="main-menu", label="🏠 Main Menu", type="flow", flow="main-menu"),
]
_FLOWS = [
    FlowNode(id="main-menu", say="What can I help with?", buttons=["browse"]),
    FlowNode(
        id="locations",
        say="You can view all our locations below in 'Browse Locations'.",
        buttons=["browse", "main-menu"],
        description="someone wants to find a clinic",
    ),
]


@pytest.fixture
def flow_hotel(hotel, override_tenant):
    tenant = hotel.model_copy(update={"links": _LINKS, "flows": _FLOWS})
    override_tenant(tenant)
    return tenant


class TestConditionalBinding:
    def test_not_bound_without_flows(self, hotel):
        assert hotel.flows == []
        assert "start_flow" not in {t.name for t in native_tools_for(hotel, "chat")}

    def test_bound_on_chat_when_flows_exist(self, flow_hotel):
        assert "start_flow" in {t.name for t in native_tools_for(flow_hotel, "chat")}

    def test_never_bound_on_voice(self, flow_hotel):
        assert "start_flow" not in {t.name for t in native_tools_for(flow_hotel, "voice")}

    def test_not_a_slow_tool(self):
        # Pure in-memory config lookup — an acknowledgement before it would
        # be a stall for something that never touches the network.
        assert "start_flow" not in SLOW_TOOLS


class TestDirectInvocation:
    async def test_returns_the_nodes_own_wording(self, flow_hotel):
        result = await start_flow.ainvoke(
            {"flow_id": "locations"}, config=tool_config(flow_hotel.tenant_id)
        )
        assert result == "You can view all our locations below in 'Browse Locations'."

    async def test_an_unknown_flow_lists_the_real_ones_instead_of_raising(self, flow_hotel):
        result = await start_flow.ainvoke(
            {"flow_id": "nope"}, config=tool_config(flow_hotel.tenant_id)
        )
        assert "main-menu" in result and "locations" in result

    async def test_tenant_a_cannot_start_tenant_bs_flow(self, flow_hotel, northside):
        result = await start_flow.ainvoke(
            {"flow_id": "locations"}, config=tool_config(northside.tenant_id)
        )
        assert "No flow called 'locations'" in result


class TestGraphTermination:
    async def test_the_model_gets_no_turn_after_a_flow_renders(self, flow_hotel, scripted):
        """`app/brain/graph.py::_after_tools` routes a `kind: "flow"`
        artifact to END. Scripting only ONE model response proves it: if the
        graph looped back to `reason`, ScriptedChatModel would raise "ran out
        of responses" instead of the turn ending cleanly."""
        model = scripted(ai("", [{"name": "start_flow", "args": {"flow_id": "locations"}}]))

        events = [
            event
            async for event in stream_turn(
                text="I need to find a clinic near me",
                tenant_id=flow_hotel.tenant_id,
                session_id="start-flow-terminal",
            )
        ]

        assert model.cursor == 1  # one request in, none after the tool
        assert not any(e.type == "error" for e in events)
        spoken = "".join(e.text for e in events if e.is_spoken)
        assert spoken == "You can view all our locations below in 'Browse Locations'."
        actions = [e for e in events if e.type == "actions"]
        assert [b["slug"] for b in actions[0].data["actions"]] == ["browse", "main-menu"]

    async def test_an_unknown_flow_does_NOT_terminate_the_turn(self, flow_hotel, scripted):
        """The recovery path: no artifact means no END, so the model gets
        the "available flows" text back and can answer properly."""
        model = scripted(
            ai("", [{"name": "start_flow", "args": {"flow_id": "typo"}}]),
            ai("Sorry — did you mean finding a location?"),
        )

        events = [
            event
            async for event in stream_turn(
                text="take me somewhere",
                tenant_id=flow_hotel.tenant_id,
                session_id="start-flow-recover",
            )
        ]

        assert model.cursor == 2
        assert "did you mean" in "".join(e.text for e in events if e.is_spoken)

    async def test_an_ordinary_tool_still_returns_to_reason(self, flow_hotel, scripted):
        """The regression guard for the new conditional edge: every non-flow
        artifact must behave exactly as it did before Phase 9.2."""
        model = scripted(
            ai(
                "One moment.",
                [{"name": "check_availability", "args": {"service": "room-reservation"}}],
            ),
            ai("Here's what I found."),
        )

        events = [
            event
            async for event in stream_turn(
                text="any rooms tomorrow?",
                tenant_id=flow_hotel.tenant_id,
                session_id="start-flow-normal-tool",
            )
        ]

        assert model.cursor == 2
        assert "Here's what I found." in "".join(e.text for e in events if e.is_spoken)


def test_a_flow_reaches_a_real_chat_sse_stream(flow_hotel, scripted):
    scripted(ai("", [{"name": "start_flow", "args": {"flow_id": "locations"}}]))
    with TestClient(app) as client:
        session = client.post(
            "/chat/session", json={"widget_key": "pk_widget_hotelmzv_demo"}
        ).json()
        response = client.post(
            "/chat",
            json={"message": "where are you based?"},
            headers={"Authorization": f"Bearer {session['token']}"},
        )

    payloads = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert "Browse Locations" in "".join(p["text"] for p in payloads if p["type"] == "token")
    assert any(p["type"] == "actions" for p in payloads)
    # tool_start/tool_result stay internal, same as every other tool.
    assert not any(p["type"] in ("tool_start", "tool_result") for p in payloads)
