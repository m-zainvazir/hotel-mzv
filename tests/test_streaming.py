"""Streaming + acknowledge-then-act (CLAUDE.md convention #1, plan §13).

These are latency tests without a stopwatch: they assert the *shape* of the
event stream, which is what protects the 600-800ms budget.
"""

from __future__ import annotations

from app.brain.runner import stream_turn
from tests.conftest import ai


async def collect(**kwargs):
    return [event async for event in stream_turn(**kwargs)]


async def test_tokens_stream_rather_than_arriving_in_one_lump(scripted, hotel):
    scripted(ai("Absolutely, we can help you with that today."))

    events = await collect(text="can you help?", tenant_id=hotel.tenant_id, session_id="s")
    tokens = [e for e in events if e.type == "token"]

    assert len(tokens) > 3, "reply arrived whole — the model was not streamed"
    assert all(t.text for t in tokens)


async def test_final_event_equals_everything_that_was_spoken(scripted, hotel):
    scripted(ai("Sure thing, let me take a look."))

    events = await collect(text="hi", tenant_id=hotel.tenant_id, session_id="s")
    spoken = "".join(e.text for e in events if e.is_spoken)
    final = next(e for e in events if e.type == "final")

    assert final.text == spoken
    assert events[-1].type == "final"


async def test_silence_before_a_slow_tool_is_filled(scripted, hotel):
    """The model said nothing before calling a tool — the runner covers for it."""
    scripted(
        ai("", [{"name": "check_availability", "args": {"service": "panel upgrade"}}]),
        ai("Here's what I've got."),
    )

    events = await collect(text="I need a panel upgrade", tenant_id=hotel.tenant_id, session_id="s")
    types = [e.type for e in events]

    assert "acknowledgement" in types
    ack_index = types.index("acknowledgement")
    tool_index = types.index("tool_start")
    assert ack_index < tool_index, "the caller must hear something before the tool runs"

    ack = events[ack_index]
    assert ack.text.strip()
    assert ack.tool == "check_availability"
    assert ack.text.endswith(" "), "without a separator it runs into the next sentence"


async def test_consecutive_fillers_do_not_collide(scripted, hotel):
    """Two silent tool calls in a row must not produce 'the diary.I'll text you'."""
    scripted(
        ai("", [{"name": "book_job", "args": {"service": "diagnostic-visit"}}]),
        ai("", [{"name": "send_confirmation", "args": {"job_id": "job_x"}}]),
        ai("All done."),
    )

    events = await collect(text="book it", tenant_id=hotel.tenant_id, session_id="s")
    said = next(e for e in events if e.type == "final").text

    assert ".I" not in said
    assert not any(
        left.isalpha() and right.isupper()
        for left, right in zip(said, said[1:], strict=False)
        if left in ".!?"
    )


async def test_no_filler_when_the_model_already_spoke(scripted, hotel):
    """Acknowledge-then-act is the model's job first; the runner only backstops."""
    scripted(
        ai(
            "Sure, let me check the diary. ",
            [{"name": "check_availability", "args": {"service": "panel upgrade"}}],
        ),
        ai("Here's what I've got."),
    )

    events = await collect(text="I need a panel upgrade", tenant_id=hotel.tenant_id, session_id="s")

    assert not any(e.type == "acknowledgement" for e in events)
    assert events[0].type == "token"


async def test_something_is_spoken_before_every_slow_tool_call(scripted, hotel):
    """The invariant, across a multi-hop turn: never dead air before a tool."""
    scripted(
        ai("", [{"name": "check_availability", "args": {"service": "diagnostic-visit"}}]),
        ai("", [{"name": "check_availability", "args": {"service": "panel-upgrade"}}]),
        ai("Right, here are your options."),
    )

    events = await collect(text="what's free this week?", tenant_id=hotel.tenant_id, session_id="s")

    spoken_before = 0
    for event in events:
        if event.is_spoken:
            spoken_before += 1
        elif event.type == "tool_start":
            assert spoken_before > 0, f"{event.tool} ran with nothing spoken beforehand"
            spoken_before = 0


async def test_tool_results_are_reported_but_not_spoken(scripted, hotel):
    scripted(
        ai("Checking. ", [{"name": "check_availability", "args": {"service": "diagnostic-visit"}}]),
        ai("Got it."),
    )

    events = await collect(text="anything today?", tenant_id=hotel.tenant_id, session_id="s")
    results = [e for e in events if e.type == "tool_result"]

    assert len(results) == 1
    assert results[0].tool == "check_availability"
    assert not results[0].is_spoken

    final = next(e for e in events if e.type == "final")
    assert "slot_start_iso" not in final.text, "raw tool output must never reach the caller"
