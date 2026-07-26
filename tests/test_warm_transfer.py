"""Vapi warm transfer (Phase 3): channel-aware escalator selection, the
handoff event through the graph, and the assistant payload's transferCall
declaration. Wire-format-specific SSE assertions live in test_vapi_llm.py."""

from __future__ import annotations

from app.brain.acknowledge import acknowledgement_for
from app.brain.runner import stream_turn
from app.channels.vapi_provisioning import build_assistant_payload
from app.config import Settings
from app.tools.messaging.transfer import SmsCallbackEscalator, WarmTransferEscalator
from app.tools.messaging_tools import escalate
from app.tools.providers import get_escalator
from tests.conftest import ai, tool_config


async def collect(**kwargs):
    return [event async for event in stream_turn(**kwargs)]


# --- escalator selection -----------------------------------------------------


def test_voice_gets_a_warm_transfer_escalator_by_default(hotel):
    assert isinstance(get_escalator(hotel, "voice"), WarmTransferEscalator)


def test_chat_gets_an_sms_callback_escalator(hotel):
    assert isinstance(get_escalator(hotel, "chat"), SmsCallbackEscalator)


def test_allow_warm_transfer_false_falls_back_to_sms_even_on_voice(hotel):
    tenant = hotel.model_copy(
        update={"emergency": hotel.emergency.model_copy(update={"allow_warm_transfer": False})}
    )
    assert isinstance(get_escalator(tenant, "voice"), SmsCallbackEscalator)


def test_no_escalation_phone_falls_back_to_sms_even_on_voice(hotel):
    tenant = hotel.model_copy(
        update={"emergency": hotel.emergency.model_copy(update={"escalation_phone": ""})}
    )
    assert isinstance(get_escalator(tenant, "voice"), SmsCallbackEscalator)


# --- escalate() still returns a plain string to a bare invoke ---------------


async def test_escalate_bare_invoke_still_returns_a_plain_string(hotel):
    """Regression guard for the content_and_artifact switch: every existing
    caller that does `.ainvoke({...})` directly (not through a ToolNode tool
    call) must keep getting a plain string, never a tuple."""
    result = await escalate.ainvoke(
        {"reason": "test"}, config=tool_config(hotel.tenant_id, channel="voice")
    )
    assert isinstance(result, str)
    assert not isinstance(result, tuple)


# --- the handoff event through the graph -------------------------------------


async def test_chat_emergency_yields_a_handoff_event_with_transfer_false(hotel, scripted):
    """Phase 5: chat now gets a `handoff` event too (a widget can use it for a
    click-to-call button), but `data["transfer"]` is False — the real
    invariant is that only voice ever gets `transfer: True`, since that's what
    gates an actual Vapi `transferCall` frame (see test_vapi_llm.py)."""
    scripted(
        ai(
            "That sounds serious. ",
            [{"name": "escalate", "args": {"reason": "gas smell"}}],
        ),
        ai("Please call our on-call number."),
    )

    events = await collect(
        text="I smell gas", tenant_id=hotel.tenant_id, session_id="chat-handoff", channel="chat"
    )

    handoffs = [e for e in events if e.type == "handoff"]
    assert len(handoffs) == 1
    assert handoffs[0].data["transfer"] is False
    assert handoffs[0].is_spoken is False


async def test_voice_emergency_yields_handoff_with_destination(hotel, scripted):
    scripted(
        ai(
            "Stay safe, help is on the way. ",
            [{"name": "escalate", "args": {"reason": "gas smell"}}],
        ),
        ai("Someone is coming to get you."),
    )

    events = await collect(
        text="I smell gas",
        tenant_id=hotel.tenant_id,
        session_id="voice-handoff-2",
        channel="voice",
    )

    handoffs = [e for e in events if e.type == "handoff"]
    assert len(handoffs) == 1
    assert handoffs[0].data["destination"] == hotel.emergency.escalation_phone
    assert handoffs[0].data["transfer"] is True
    assert handoffs[0].is_spoken is False


# --- assistant payload --------------------------------------------------------


def _provisioning_settings(**overrides) -> Settings:
    base = {
        "public_base_url": "https://test.ngrok.app",
        "vapi_private_key": "vapi_test",
        "vapi_webhook_secret": "sh4red",
        "cartesia_default_voice_id": "voice_default",
    }
    base.update(overrides)
    return Settings(**base)


def test_payload_declares_transfer_call_tool_when_enabled(hotel):
    payload = build_assistant_payload(hotel, settings=_provisioning_settings())
    tools = payload["model"]["tools"]
    assert tools == [
        {
            "type": "transferCall",
            "destinations": [{"type": "number", "number": hotel.emergency.escalation_phone}],
        }
    ]


def test_payload_omits_transfer_call_tool_when_disabled(hotel):
    tenant = hotel.model_copy(
        update={"emergency": hotel.emergency.model_copy(update={"allow_warm_transfer": False})}
    )
    payload = build_assistant_payload(tenant, settings=_provisioning_settings())
    assert "tools" not in payload["model"]


def test_escalation_number_appears_exactly_once_in_the_payload(hotel):
    payload = build_assistant_payload(hotel, settings=_provisioning_settings())
    import json

    text = json.dumps(payload)
    assert text.count(hotel.emergency.escalation_phone) == 1


# --- channel-scoped acknowledgements -----------------------------------------


def test_chat_acknowledgement_never_promises_a_transfer():
    from app.brain.acknowledge import seed

    seed(1)
    for _ in range(20):
        line = acknowledgement_for("escalate", "chat")
        assert "putting you through" not in line.lower()
        assert "transferring" not in line.lower()


def test_voice_acknowledgement_can_promise_a_transfer():
    from app.brain.acknowledge import seed

    seed(1)
    lines = {acknowledgement_for("escalate", "voice") for _ in range(20)}
    assert any("putting you through" in line.lower() for line in lines)
