"""Suppressing a re-spoken acknowledgement (observed live on Llama 3.3).

Having said "Let me check what we've got" and called a tool, the model opens
its follow-up with that same sentence. Fine in text, jarring on a phone call.
"""

from __future__ import annotations

from app.brain.runner import stream_turn
from app.brain.sanitize import RepeatSuppressor
from tests.conftest import ai


def stream(target: str, chunks: list[str]) -> str:
    suppressor = RepeatSuppressor()
    suppressor.arm(target)
    return "".join(suppressor.feed(c) for c in chunks) + suppressor.flush()


def test_repeated_opening_is_dropped():
    said = stream(
        "Let me check what we've got. ",
        ["Let me ", "check ", "what ", "we've ", "got. ", "We have ", "9am free."],
    )
    assert said == "We have 9am free."


def test_truncated_restatement_is_dropped():
    """The shape actually observed on a live Groq call."""
    said = stream(
        "Let me check what we've got available for you. ",
        ["Let me ", "check ", "what we've got… ", "We've got a few options, ", "how about one?"],
    )
    assert said == "We've got a few options, how about one?"


def test_extended_restatement_is_dropped():
    """The other direction — the repeat is longer than the original."""
    said = stream(
        "Let me check.",
        ["Let me check the diary for you. ", "Tuesday at nine is free."],
    )
    assert said == "Tuesday at nine is free."


def test_reworded_restatement_is_dropped():
    said = stream(
        "One second, booking that in now.",
        ["One second, booking that in now! ", "All done."],
    )
    assert said == "All done."


def test_repeat_split_awkwardly_across_chunks_is_still_dropped():
    said = stream("One second please.", ["On", "e sec", "ond please. Booking ", "that now."])
    assert said == "Booking that now."


def test_genuinely_new_wording_is_untouched():
    said = stream("Let me check what we've got.", ["We have ", "three ", "slots ", "free."])
    assert said == "We have three slots free."


def test_a_near_miss_is_not_swallowed():
    """Diverges partway — every character must still be spoken."""
    said = stream("Let me check the diary.", ["Let me ", "see what ", "I can do."])
    assert said == "Let me see what I can do."


def test_only_the_first_sentence_is_ever_considered():
    """A later sentence that echoes the acknowledgement is left alone."""
    said = stream(
        "Let me check what we've got.",
        ["Tuesday at nine works. ", "Let me check what we've got for Wednesday too."],
    )
    assert said == "Tuesday at nine works. Let me check what we've got for Wednesday too."


def test_a_segment_that_is_entirely_a_repeat_says_nothing():
    assert stream("One moment please.", ["One ", "moment ", "please."]) == ""


def test_partial_repeat_at_end_of_stream_is_dropped():
    assert stream("One moment please.", ["One ", "moment"]) == ""


def test_short_openers_are_never_swallowed():
    """ "Okay." carries no duplicated information and is a normal way to start."""
    assert stream("Okay.", ["Okay. ", "Tuesday works."]) == "Okay. Tuesday works."


def test_holding_is_bounded():
    """A long non-repeat must not be held hostage waiting for a full stop."""
    target = "Let me check what we've got available for you right now"
    long_tail = target + " and here is a great deal more text with no terminator " * 4
    assert long_tail.startswith(target)
    assert stream(target, [long_tail]) != ""


def test_unarmed_suppressor_passes_everything_through():
    suppressor = RepeatSuppressor()
    assert suppressor.feed("anything at all") == "anything at all"
    assert suppressor.flush() == ""


def test_arming_with_nothing_disables_it():
    suppressor = RepeatSuppressor()
    suppressor.arm("   ")
    assert suppressor.feed("hello") == "hello"


async def test_end_to_end_the_caller_does_not_hear_it_twice(scripted, hotel):
    """The real shape: acknowledge → tool → model repeats the acknowledgement."""
    scripted(
        ai(
            "Let me check what we've got. ",
            [{"name": "check_availability", "args": {"service": "diagnostic-visit"}}],
        ),
        ai("Let me check what we've got. We have a few slots this afternoon."),
    )

    events = [
        e
        async for e in stream_turn(
            text="can someone come out?",
            tenant_id=hotel.tenant_id,
            session_id="repeat",
            channel="voice",
        )
    ]

    said = next(e for e in events if e.type == "final").text
    assert said.count("Let me check what we've got") == 1
    assert "We have a few slots this afternoon." in said
