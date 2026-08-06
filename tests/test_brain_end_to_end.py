"""Phase 1 acceptance criterion, as a test.

A caller describes a fault, gets offered real slots, gives their details, and
walks away with a booked job and a confirmation text — driven entirely through
the graph, with tokens streaming the whole way.
"""

from __future__ import annotations

import re

from langchain_core.messages import BaseMessage, ToolMessage

from app.brain.runner import stream_turn
from app.db.memory_store import get_store
from tests.conftest import ai


def _last_tool_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            return str(message.content)
    return ""


def _slot_iso(messages: list[BaseMessage]) -> str:
    """Pick the first slot the availability tool offered earlier this conversation."""
    for message in messages:
        if isinstance(message, ToolMessage):
            found = re.search(r"slot_start_iso=([^)\s]+)", str(message.content))
            if found:
                return found.group(1)
    raise AssertionError("no slot_start_iso in conversation history")


def _job_id(messages: list[BaseMessage]) -> str:
    found = re.search(r"job_id=(\S+)", _last_tool_text(messages))
    assert found, "book_job did not return a job id"
    return found.group(1)


async def collect(**kwargs):
    return [event async for event in stream_turn(**kwargs)]


async def test_books_a_job_end_to_end(scripted, hotel):
    scripted(
        # turn 1 — acknowledge, then look up real availability
        ai(
            "Sure, let me check what we've got open. ",
            [{"name": "check_availability", "args": {"service": "room-reservation"}}],
        ),
        ai("I've got a few slots. Can I take your name and number?"),
        # turn 2 — book, confirm, close out
        lambda messages: ai(
            "Great, booking that in now. ",
            [
                {
                    "name": "book_job",
                    "args": {
                        "service": "room-reservation",
                        "slot_start_iso": _slot_iso(messages),
                        "customer_name": "Dana Reyes",
                        "customer_phone": "555-111-2222",
                        "notes": "arriving after 9pm",
                    },
                }
            ],
        ),
        lambda messages: ai(
            "", [{"name": "send_confirmation", "args": {"job_id": _job_id(messages)}}]
        ),
        ai("You're all set — confirmation text is on its way."),
    )

    turn1 = await collect(
        text="do you have a room available tonight?",
        tenant_id=hotel.tenant_id,
        session_id="e2e",
    )
    assert [e.tool for e in turn1 if e.type == "tool_start"] == ["check_availability"]
    assert "slot_start_iso=" in next(e.text for e in turn1 if e.type == "tool_result")

    turn2 = await collect(
        text="Dana Reyes, 555-111-2222. The first one works.",
        tenant_id=hotel.tenant_id,
        session_id="e2e",
    )

    assert [e.tool for e in turn2 if e.type == "tool_start"] == ["book_job", "send_confirmation"]

    # the job actually exists, on the right tenant
    jobs = get_store().list_jobs(hotel.tenant_id)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.customer_name == "Dana Reyes"
    assert job.customer_phone == "+15551112222"
    assert job.service_slug == "room-reservation"
    assert hotel.is_open_at(job.scheduled_start)

    # and the caller was texted
    confirmations = [
        m for m in get_store().list_messages(hotel.tenant_id) if m.kind == "confirmation"
    ]
    assert len(confirmations) == 1
    assert confirmations[0].to == "+15551112222"

    final = next(e for e in turn2 if e.type == "final")
    assert "all set" in final.text


async def test_conversation_memory_survives_between_turns(scripted, hotel):
    model = scripted(ai("Hi Dana, what's the problem?"), ai("Of course, Dana."))

    await collect(text="Hi, this is Dana", tenant_id=hotel.tenant_id, session_id="mem")
    await collect(text="do you remember my name?", tenant_id=hotel.tenant_id, session_id="mem")

    second_prompt = model.seen_prompts[-1]
    assert any("this is Dana" in str(m.content) for m in second_prompt)


async def test_emergency_turn_reaches_the_model_pre_flagged(scripted, hotel):
    model = scripted(
        ai(
            "That sounds serious — stay safe. Putting you through now. ",
            [{"name": "escalate", "args": {"reason": "smoke in the corridor"}}],
        ),
        ai("Someone is on the way."),
    )

    events = await collect(
        text="there's smoke coming from the corridor",
        tenant_id=hotel.tenant_id,
        session_id="emergency",
        channel="voice",
    )

    system_prompt = str(model.seen_prompts[0][0].content)
    assert "EMERGENCY CLASSIFIER" in system_prompt

    assert [e.tool for e in events if e.type == "tool_start"] == ["escalate"]
    assert len(get_store().list_escalations(hotel.tenant_id)) == 1
    assert not get_store().list_jobs(hotel.tenant_id)


async def test_tenant_config_reaches_the_system_prompt(scripted, northside):
    model = scripted(ai("Northside Plumbing, what's up?"))

    await collect(text="hello", tenant_id=northside.tenant_id, session_id="prompt")

    system_prompt = str(model.seen_prompts[0][0].content)
    assert "Northside Plumbing" in system_prompt
    assert "plumber" in system_prompt
    assert "drain-clearing" in system_prompt
    assert "America/Chicago" in system_prompt


async def test_model_is_bound_to_the_native_tool_tier(scripted, hotel):
    """Chat binds the fixed five plus the two presentation tools every chat
    bot gets unconditionally (Phase 9.2) — `search_knowledge` and
    `start_flow` stay conditional and hotel-mzv has neither."""
    model = scripted(ai("Hello."))
    await collect(text="hi", tenant_id=hotel.tenant_id, session_id="bind")

    assert set(model.bound_tool_names) == {
        "check_availability",
        "book_job",
        "send_confirmation",
        "escalate",
        "is_emergency",
        "offer_actions",
        "offer_cards",
    }


async def test_voice_is_bound_to_the_fixed_five_and_nothing_else(scripted, hotel):
    """The mirror: neither presentation tool exists down a phone line."""
    model = scripted(ai("Hello."))
    await collect(text="hi", tenant_id=hotel.tenant_id, session_id="bind-voice", channel="voice")

    assert set(model.bound_tool_names) == {
        "check_availability",
        "book_job",
        "send_confirmation",
        "escalate",
        "is_emergency",
    }


async def test_rejected_tool_call_is_retried_without_tools(scripted, hotel):
    """Groq rejects malformed tool calls server-side — the caller must not hear it."""
    model = scripted(
        RuntimeError("Failed to call a function. Please adjust your prompt."),
        ai("Sorry about that — what's the address?"),
    )

    events = await collect(text="book me in", tenant_id=hotel.tenant_id, session_id="retry")

    assert not any(e.type == "error" for e in events)
    final = next(e for e in events if e.type == "final")
    assert "what's the address" in final.text

    # the retry is told not to try tools again this turn
    assert "rejected by the provider" in str(model.seen_prompts[-1][-1].content)


async def test_total_model_outage_still_says_something(scripted, hotel):
    """Silence is the worst outcome on a live call."""
    scripted(
        RuntimeError("groq is down"),
        RuntimeError("groq is still down"),
    )

    events = await collect(text="hello", tenant_id=hotel.tenant_id, session_id="outage")

    assert not any(e.type == "error" for e in events)
    final = next(e for e in events if e.type == "final")
    assert final.text.strip()
    assert "say it again" in final.text


async def test_runner_reports_an_error_event_if_the_graph_itself_fails(hotel, monkeypatch):
    class ExplodingGraph:
        def astream(self, *args, **kwargs):
            raise RuntimeError("checkpointer unavailable")

    monkeypatch.setattr("app.brain.runner.get_graph", lambda: ExplodingGraph())

    events = await collect(text="hello", tenant_id=hotel.tenant_id, session_id="boom")

    assert [e.type for e in events] == ["error"]
    assert "checkpointer unavailable" in events[0].text
