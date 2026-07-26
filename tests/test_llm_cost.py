"""Provider request accounting and the levers that reduce it.

Requests, not turns, are what a provider bills and rate-limits. One turn costs
one request, plus one per tool hop, plus one per retry — so a booking
conversation is easily 10+. These tests pin the arithmetic down so a change
that quietly doubles it gets noticed.
"""

from __future__ import annotations

import pytest

from app.brain.metrics import TurnCounter, total_llm_requests
from app.brain.nodes.reason import _worth_retrying
from app.brain.runner import stream_turn
from tests.conftest import ai


async def collect(**kwargs):
    return [event async for event in stream_turn(**kwargs)]


async def test_a_plain_turn_costs_one_request(scripted, hotel):
    scripted(ai("We open at eight."))

    events = await collect(text="what time do you open?", tenant_id=hotel.tenant_id, session_id="c")

    assert next(e for e in events if e.type == "final").data["llm_requests"] == 1


async def test_each_tool_hop_costs_another_request(scripted, hotel):
    """This is the real driver of request counts, and it is easy to miss."""
    scripted(
        ai("Checking. ", [{"name": "check_availability", "args": {"service": "diagnostic-visit"}}]),
        ai("Booking. ", [{"name": "book_job", "args": {"service": "diagnostic-visit"}}]),
        ai("All set."),
    )

    events = await collect(text="book me in", tenant_id=hotel.tenant_id, session_id="c")

    # 1 initial + 1 after each of the two tool results.
    assert next(e for e in events if e.type == "final").data["llm_requests"] == 3


async def test_a_recoverable_failure_costs_one_extra_request(scripted, hotel):
    scripted(RuntimeError("tool call validation failed"), ai("Sorry, say that again?"))

    events = await collect(text="hello", tenant_id=hotel.tenant_id, session_id="c")

    assert next(e for e in events if e.type == "final").data["llm_requests"] == 2


async def test_a_quota_failure_does_not_burn_a_second_request(scripted, hotel):
    """Retrying a 429 immediately fails again — pure waste when already capped."""
    scripted(RuntimeError("Error code: 429 - rate_limit_exceeded: tokens per day"))

    events = await collect(text="hello", tenant_id=hotel.tenant_id, session_id="c")

    final = next(e for e in events if e.type == "final")
    assert final.data["llm_requests"] == 1, "must not retry a quota error"
    assert final.text.strip(), "the caller still hears something"


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 429 - rate_limit_exceeded",
        "Rate limit reached for model llama-3.3-70b",
        "insufficient_quota",
        "Error code: 401 - invalid_api_key",
        "authentication failed",
    ],
)
def test_unretryable_failures_are_recognised(message):
    assert not _worth_retrying(RuntimeError(message))


@pytest.mark.parametrize(
    "message",
    [
        "tool call validation failed: attempted to call tool 'check_availability'",
        "Failed to call a function. Please adjust your prompt.",
        "connection reset by peer",
    ],
)
def test_recoverable_failures_are_retried(message):
    assert _worth_retrying(RuntimeError(message))


async def test_the_counter_survives_langgraph_task_boundaries(scripted, hotel):
    """The model runs inside LangGraph's own task; a ContextVar set out here
    does not travel into it. An earlier implementation reported zero always."""
    before = total_llm_requests()
    counter = TurnCounter()

    scripted(ai("Hello."))
    await collect(text="hi", tenant_id=hotel.tenant_id, session_id="c")

    assert counter.requests == 1
    assert total_llm_requests() == before + 1


# --- bounding what we re-send -----------------------------------------------


async def test_history_is_trimmed_so_long_calls_stop_compounding(scripted, hotel, monkeypatch):
    """Every request re-sends the transcript; unbounded it dwarfs the prompt."""
    monkeypatch.setenv("LLM_HISTORY_MESSAGES", "6")
    from app.config import reset_settings_cache

    reset_settings_cache()

    model = scripted(*[ai(f"reply {i}") for i in range(12)])
    for i in range(12):
        await_events = stream_turn(
            text=f"message {i}", tenant_id=hotel.tenant_id, session_id="long"
        )
        async for _ in await_events:
            pass

    last_prompt = model.seen_prompts[-1]
    # 1 system message + at most the configured history window.
    assert len(last_prompt) <= 7, f"prompt grew to {len(last_prompt)} messages"
    assert "message 11" in " ".join(str(m.content) for m in last_prompt)
    assert "message 0" not in " ".join(str(m.content) for m in last_prompt)


async def test_short_conversations_are_not_trimmed(scripted, hotel):
    model = scripted(ai("one"), ai("two"))

    for text in ("first", "second"):
        async_gen = stream_turn(text=text, tenant_id=hotel.tenant_id, session_id="short")
        async for _ in async_gen:
            pass

    rendered = " ".join(str(m.content) for m in model.seen_prompts[-1])
    assert "first" in rendered and "second" in rendered


async def test_a_model_that_cannot_call_tools_screams(scripted, hotel, caplog):
    """Silent degradation is the danger: it chats fine and books nothing."""
    scripted(
        RuntimeError("Error code: 400 - `tool calling` is not supported with this model"),
        ai("Hello there."),
    )

    with caplog.at_level("CRITICAL", logger="app.brain.nodes.reason"):
        await collect(text="book me in", tenant_id=hotel.tenant_id, session_id="notools")

    critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert critical, "a model that cannot book must not fail quietly"
    assert "CANNOT CALL TOOLS" in critical[0].getMessage()
