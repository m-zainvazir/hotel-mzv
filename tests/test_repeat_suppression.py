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
        "Let me check the diary.",
        ["Let me check the diary for you. ", "Tuesday at nine is free."],
    )
    assert said == "Tuesday at nine is free."


def test_an_extension_that_carries_new_information_is_spoken():
    """The guard on the extension direction: "for Wednesday too" makes this a
    fresh request, not an echo, even though it starts with the exact words
    that were just said."""
    said = stream(
        "Let me check what we've got.",
        ["Let me check what we've got for Wednesday too."],
    )
    assert said == "Let me check what we've got for Wednesday too."


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


def test_a_restatement_after_an_opener_is_dropped():
    """The gap that made this a live defect. The echo lands *second*, behind a
    short opener — the old first-sentence-only check could not see it at all,
    however good the comparison was."""
    said = stream(
        "I can check with a bookseller for you.",
        ["Sure. ", "I can check with a bookseller for you. ", "They have three in stock."],
    )
    assert said == "Sure. They have three in stock."


def test_each_sentence_is_a_target_in_its_own_right():
    """The other half of the live defect. Arming with a whole multi-sentence
    segment used to compare the echo against the *concatenation*, which diluted
    the similarity below the threshold and let it through."""
    said = stream(
        "Sure, happy to help with that. I can check with a bookseller for you.",
        ["I can check with a bookseller for you. ", "They have three in stock."],
    )
    assert said == "They have three in stock."


def test_a_sentence_from_an_earlier_hop_still_suppresses_an_echo():
    """Targets accumulate for the whole turn, not just the most recent hop."""
    suppressor = RepeatSuppressor()
    suppressor.arm("Let me check the diary for you.")
    first = suppressor.feed("Tuesday at nine is free.") + suppressor.flush()
    suppressor.arm("Booking that in now.")
    second = suppressor.feed("Let me check the diary for you. All done.") + suppressor.flush()
    assert first == "Tuesday at nine is free."
    assert second == "All done."


def test_a_completion_report_survives_a_similar_promise():
    """The false positive the lower threshold would otherwise introduce, and the
    worst possible one: it's the confirmation the caller is waiting for."""
    assert stream("I'll send you a confirmation text.", ["I've sent you a confirmation text."]) == (
        "I've sent you a confirmation text."
    )
    assert stream("I can book that for you.", ["I've booked that for you."]) == (
        "I've booked that for you."
    )


def test_a_different_object_is_not_a_repeat():
    """Same wording, different day — the novel content word protects it."""
    said = stream("Let me check Tuesday for you.", ["Let me check Wednesday for you."])
    assert said == "Let me check Wednesday for you."


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


async def test_end_to_end_a_restatement_behind_an_opener_is_dropped(scripted, hotel):
    """The shape the first-sentence-only check could never catch, driven through
    the real graph: a multi-sentence pre-tool segment, and the echo arriving
    second in the follow-up."""
    scripted(
        ai(
            "Sure, happy to help. Let me check the diary for you. ",
            [{"name": "check_availability", "args": {"service": "diagnostic-visit"}}],
        ),
        ai("Right. Let me check the diary for you. Tuesday at nine is free."),
    )

    events = [
        e
        async for e in stream_turn(
            text="can someone come out?",
            tenant_id=hotel.tenant_id,
            session_id="repeat-opener",
            channel="voice",
        )
    ]

    said = next(e for e in events if e.type == "final").text
    assert said.count("Let me check the diary for you") == 1
    assert "Tuesday at nine is free." in said
