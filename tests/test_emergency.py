"""Emergency classifier + the graph node that runs it."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.brain.nodes.emergency_check import emergency_check
from app.tools.emergency import classify_emergency


@pytest.mark.parametrize(
    "utterance",
    [
        "there's smoke coming from the corridor",
        "I think there's a fire on the second floor",
        "a guest has collapsed in the lobby",
        "someone is choking in the restaurant",
    ],
)
def test_hotel_emergencies_fire(hotel, utterance):
    assert classify_emergency(utterance, hotel).is_emergency


@pytest.mark.parametrize(
    "utterance",
    [
        "I'd like to book a room for next weekend",
        "can you tell me about your spa treatments",
        "how much is the event space",
        "",
    ],
)
def test_routine_requests_do_not_fire(hotel, utterance):
    assert not classify_emergency(utterance, hotel).is_emergency


def test_patterns_are_per_trade(hotel, northside):
    """A plumber's emergencies are not a hotel's, and vice versa."""
    burst_pipe = "there's a burst pipe in the basement"
    assert classify_emergency(burst_pipe, northside).is_emergency
    assert not classify_emergency(burst_pipe, hotel).is_emergency

    stuck = "a guest is stuck in the elevator"
    assert classify_emergency(stuck, hotel).is_emergency
    assert not classify_emergency(stuck, northside).is_emergency


def test_verdict_reports_what_matched(hotel):
    verdict = classify_emergency("there's a fire and I can smell smoke", hotel)
    assert verdict.is_emergency
    assert "fire" in verdict.reason
    assert "smoke" in verdict.reason


async def test_node_sets_flag_and_intent(hotel):
    state = {
        "tenant_id": hotel.tenant_id,
        "messages": [HumanMessage(content="there's smoke in the corridor")],
    }
    update = await emergency_check(state)
    assert update["emergency"] is True
    assert update["intent"] == "emergency"
    assert update["emergency_reason"]


async def test_node_never_clears_a_raised_flag(hotel):
    """Once a call is an emergency, a calm follow-up doesn't make it safe."""
    state = {
        "tenant_id": hotel.tenant_id,
        "emergency": True,
        "messages": [HumanMessage(content="ok, thanks")],
    }
    assert await emergency_check(state) == {}


async def test_node_ignores_turns_with_no_caller_message(hotel):
    state = {"tenant_id": hotel.tenant_id, "messages": [AIMessage(content="hello?")]}
    assert await emergency_check(state) == {}
