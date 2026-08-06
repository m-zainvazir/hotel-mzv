"""Deterministic flows (Phase 9.2) — the engine, the postback path, the menu.

The point of the whole feature is that a clicked button produces a fixed
answer with no model involved, so the load-bearing assertions here are about
what *doesn't* happen: no LLM request, no chance for the model to add a
trailing sentence, no cross-tenant reach.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.brain.runner import stream_turn
from app.flows.resolver import parse_postback, resolve_buttons, resolve_flow, resolve_menu
from app.main import app
from app.tenancy.models import FlowNode, TenantLink
from tests.conftest import ai

_LINKS = [
    {"slug": "book", "label": "📅 Book an Appointment", "url": "https://ppt.example.com/book"},
    {"slug": "locations", "label": "📍 Find a Location", "type": "flow", "flow": "locations"},
    {"slug": "main-menu", "label": "🏠 Main Menu", "type": "flow", "flow": "main-menu"},
    {"slug": "call-us", "label": "📞 Talk to someone", "type": "handoff"},
    {"slug": "zip", "label": "Share my zip code", "type": "reply", "value": "my zip code is"},
]
_FLOWS = [
    {
        "id": "main-menu",
        "say": "What can I help you with today?",
        "buttons": ["book", "locations", "call-us"],
        "description": "the top-level menu",
    },
    {
        "id": "locations",
        "say": "You can view all our locations below in 'Browse Locations'.",
        "buttons": ["book", "main-menu"],
        "description": "someone wants to find a clinic",
    },
    {"id": "bare", "say": "Nothing to click here."},
]


def _configured(tenant, *, menu_flow: str | None = "main-menu"):
    return tenant.model_copy(
        update={
            "links": [TenantLink(**link) for link in _LINKS],
            "flows": [FlowNode(**flow) for flow in _FLOWS],
            "chat": tenant.chat.model_copy(update={"menu_flow": menu_flow}),
        }
    )


@pytest.fixture
def flow_hotel(hotel, override_tenant):
    tenant = _configured(hotel)
    override_tenant(tenant)
    return tenant


class TestResolver:
    def test_resolves_a_node_and_its_buttons_in_order(self, flow_hotel):
        resolved = resolve_flow(flow_hotel, "main-menu")
        assert resolved is not None
        assert resolved.node.say == "What can I help you with today?"
        assert [b["slug"] for b in resolved.buttons] == ["book", "locations", "call-us"]

    def test_a_missing_node_is_none_not_an_error(self, flow_hotel):
        # A stale button in a tab left open across a deploy must degrade to
        # an ordinary model turn, never a dead end.
        assert resolve_flow(flow_hotel, "deleted-flow") is None

    def test_every_row_carries_every_key(self, flow_hotel):
        for row in resolve_buttons(flow_hotel, ["book", "locations", "call-us", "zip"]):
            assert set(row) == {"type", "label", "slug", "url", "value", "flow"}

    def test_a_reply_button_carries_its_value_a_link_does_not(self, flow_hotel):
        rows = {r["slug"]: r for r in resolve_buttons(flow_hotel, ["zip", "book", "call-us"])}
        assert rows["zip"]["value"] == "my zip code is"
        assert rows["book"]["value"] is None
        # handoff with no explicit value falls back to the label
        assert rows["call-us"]["value"] == "📞 Talk to someone"

    def test_unknown_slugs_are_dropped_not_fatal(self, flow_hotel):
        rows = resolve_buttons(flow_hotel, ["book", "nope", "call-us"])
        assert [r["slug"] for r in rows] == ["book", "call-us"]

    def test_the_menu_is_the_menu_flows_own_buttons(self, flow_hotel):
        assert [b["slug"] for b in resolve_menu(flow_hotel)] == ["book", "locations", "call-us"]

    def test_no_menu_flow_means_no_menu(self, hotel, override_tenant):
        tenant = _configured(hotel, menu_flow=None)
        override_tenant(tenant)
        assert resolve_menu(tenant) == []

    def test_a_tenant_with_no_flows_at_all_resolves_nothing(self, hotel):
        assert hotel.flows == []
        assert resolve_flow(hotel, "main-menu") is None
        assert resolve_menu(hotel) == []


class TestParsePostback:
    def test_reads_a_flow_id(self):
        assert parse_postback("flow:main-menu") == "main-menu"

    def test_anything_without_the_prefix_is_not_a_flow(self):
        # Strict on purpose: a future postback kind must not be silently
        # reinterpreted as a flow jump.
        for value in (None, "", "main-menu", "quickreply:x", "flow:"):
            assert parse_postback(value) is None


class TestPostbackTurn:
    async def test_a_flow_turn_makes_zero_llm_requests(self, flow_hotel, scripted):
        """The entire justification for a flow engine over LLM re-entry."""
        model = scripted()  # no responses scripted at all — asking for one would raise

        events = [
            event
            async for event in stream_turn(
                text="📍 Find a Location",
                tenant_id=flow_hotel.tenant_id,
                session_id="flow-zero-llm",
                postback="flow:locations",
            )
        ]

        assert model.cursor == 0
        spoken = "".join(e.text for e in events if e.is_spoken)
        assert spoken == "You can view all our locations below in 'Browse Locations'."
        actions = [e for e in events if e.type == "actions"]
        assert [b["slug"] for b in actions[0].data["actions"]] == ["book", "main-menu"]
        final = next(e for e in events if e.type == "final")
        assert final.data["llm_requests"] == 0
        assert final.data["flow_id"] == "locations"

    async def test_a_node_with_no_buttons_emits_no_actions_event(self, flow_hotel, scripted):
        scripted()
        events = [
            event
            async for event in stream_turn(
                text="bare",
                tenant_id=flow_hotel.tenant_id,
                session_id="flow-bare",
                postback="flow:bare",
            )
        ]
        assert not any(e.type == "actions" for e in events)
        assert any(e.type == "final" for e in events)

    async def test_the_flow_turn_is_visible_to_the_next_free_text_turn(self, flow_hotel, scripted):
        """The `aupdate_state` write-back (app/flows/render.py::_remember).

        Without it nothing errors — the model just quietly has amnesia about
        what the visitor navigated through, which is exactly the kind of bug
        that only shows up in a real conversation.

        Both turns are scripted in ONE `scripted()` call on purpose: that
        fixture calls `reset_graph()`, which would throw away the in-memory
        checkpointer (and with it the very state under test) if it ran
        between the two turns.
        """
        model = scripted(ai("Those were Book and Main Menu."))
        session = "flow-memory"

        async for _ in stream_turn(
            text="📍 Find a Location",
            tenant_id=flow_hotel.tenant_id,
            session_id=session,
            postback="flow:locations",
        ):
            pass
        assert model.cursor == 0  # still no LLM request from the flow turn

        async for _ in stream_turn(
            text="what were those options again?",
            tenant_id=flow_hotel.tenant_id,
            session_id=session,
        ):
            pass

        transcript = " ".join(str(getattr(m, "content", "")) for m in model.seen_prompts[-1])
        assert "Browse Locations" in transcript
        assert "📍 Find a Location" in transcript

    async def test_a_stale_postback_falls_through_to_the_model(self, flow_hotel, scripted):
        model = scripted(ai("That option has moved — can I help another way?"))

        events = [
            event
            async for event in stream_turn(
                text="Deleted Button",
                tenant_id=flow_hotel.tenant_id,
                session_id="flow-stale",
                postback="flow:no-longer-exists",
            )
        ]

        assert model.cursor == 1
        assert "moved" in "".join(e.text for e in events if e.is_spoken)

    async def test_voice_ignores_a_postback_entirely(self, flow_hotel, scripted):
        """A phone caller can't click anything, so a postback arriving on
        the voice channel is either a bug or a forgery — either way it must
        not short-circuit the graph."""
        model = scripted(ai("Sure, I can help with that."))

        events = [
            event
            async for event in stream_turn(
                text="find a location",
                tenant_id=flow_hotel.tenant_id,
                session_id="flow-voice",
                channel="voice",
                postback="flow:locations",
            )
        ]

        assert model.cursor == 1
        assert "Browse Locations" not in "".join(e.text for e in events if e.is_spoken)

    async def test_tenant_a_cannot_reach_tenant_bs_flow(self, flow_hotel, northside, scripted):
        """northside declares no flows of its own — hotel's `locations` must
        be unreachable from it, the mirror of the catalog-slug isolation
        test in test_action_tools.py."""
        model = scripted(ai("I'm not sure about that one."))

        events = [
            event
            async for event in stream_turn(
                text="📍 Find a Location",
                tenant_id=northside.tenant_id,
                session_id="flow-cross-tenant",
                postback="flow:locations",
            )
        ]

        assert model.cursor == 1  # fell through to the model, no flow rendered
        assert "Browse Locations" not in "".join(e.text for e in events if e.is_spoken)


# --- through /chat and the handshake ---------------------------------------


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _sse_payloads(body: str) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in body.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def _handshake(client, widget_key: str = "pk_widget_hotelmzv_demo") -> dict:
    response = client.post("/chat/session", json={"widget_key": widget_key})
    assert response.status_code == 200
    return response.json()


def test_the_handshake_carries_the_menu(client, flow_hotel):
    tenant = _handshake(client)["tenant"]
    assert [b["label"] for b in tenant["menu"]] == [
        "📅 Book an Appointment",
        "📍 Find a Location",
        "📞 Talk to someone",
    ]


def test_a_tenant_with_no_menu_flow_gets_an_empty_menu(client, hotel):
    assert hotel.chat.menu_flow is None
    assert _handshake(client)["tenant"]["menu"] == []


def test_a_flow_postback_streams_over_sse(client, flow_hotel, scripted):
    scripted()
    session = _handshake(client)

    response = client.post(
        "/chat",
        json={"message": "📍 Find a Location", "postback": "flow:locations"},
        headers={"Authorization": f"Bearer {session['token']}"},
    )

    assert response.status_code == 200
    payloads = _sse_payloads(response.text)
    assert "Browse Locations" in "".join(p["text"] for p in payloads if p["type"] == "token")
    actions = [p for p in payloads if p["type"] == "actions"]
    assert [b["slug"] for b in actions[0]["data"]["actions"]] == ["book", "main-menu"]


def test_an_over_long_postback_is_rejected_by_the_request_model(client, flow_hotel):
    session = _handshake(client)
    response = client.post(
        "/chat",
        json={"message": "hi", "postback": "flow:" + "x" * 500},
        headers={"Authorization": f"Bearer {session['token']}"},
    )
    assert response.status_code == 422


# --- the opening turn -------------------------------------------------------
#
# A model can't produce buttons without a turn to produce them in, so a bot
# whose operator configured nothing needs one as the panel opens. A
# configured menu renders instantly and for free, so the server suppresses
# the opening turn whenever one exists — there'd be nothing to buy.


class TestOpeningTurn:
    def test_a_zero_config_bot_is_told_to_run_one(self, client, hotel):
        assert hotel.chat.menu_flow is None
        assert hotel.ui.opening_turn is True
        tenant = _handshake(client)["tenant"]
        assert tenant["opening_turn"] is True
        assert tenant["menu"] == []

    def test_a_configured_menu_suppresses_it(self, client, flow_hotel):
        """Both would greet the visitor with buttons; only one costs an LLM
        request, so the configured one wins."""
        assert flow_hotel.ui.opening_turn is True
        tenant = _handshake(client)["tenant"]
        assert tenant["menu"] != []
        assert tenant["opening_turn"] is False

    def test_it_can_be_switched_off(self, client, hotel, override_tenant):
        from app.tenancy.models import UiSettings

        override_tenant(hotel.model_copy(update={"ui": UiSettings(opening_turn=False)}))
        assert _handshake(client)["tenant"]["opening_turn"] is False
