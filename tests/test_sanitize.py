"""Leaked-tool-call recovery (plan §17 — Groq tool-calling reliability).

Observed live: Llama 3.3 sometimes writes
`<function=send_confirmation>{"job_id": "job_x"}</function>` into the reply
text. It must never be spoken, and the call it describes must still run.
"""

from __future__ import annotations

from app.brain.runner import stream_turn
from app.brain.sanitize import InlineToolCallFilter, extract_inline_tool_calls, extract_urls
from app.db.memory_store import get_store
from tests.conftest import ai

LEAK = '<function=send_confirmation>{"job_id": "job_abc"}</function>'


def test_extracts_a_leaked_call_and_strips_the_markup():
    cleaned, calls = extract_inline_tool_calls(f"All booked. {LEAK}")

    assert cleaned == "All booked."
    assert calls == [
        {
            "name": "send_confirmation",
            "args": {"job_id": "job_abc"},
            "id": "inline_0",
            "type": "tool_call",
        }
    ]


def test_tolerates_a_missing_closing_tag():
    cleaned, calls = extract_inline_tool_calls(
        'Booking now. <function=book_job{"service": "panel-upgrade"}'
    )
    assert cleaned == "Booking now."
    assert calls[0]["name"] == "book_job"


def test_malformed_json_is_dropped_not_guessed():
    cleaned, calls = extract_inline_tool_calls('Hi. <function=book_job>{"service": </function>')
    assert calls == []
    assert "<function" not in cleaned


def test_ordinary_text_passes_through_untouched():
    text = "We can be there at 9am — that's a 60 minute visit."
    assert extract_inline_tool_calls(text) == (text, [])


def _stream(chunks: list[str]) -> str:
    filt = InlineToolCallFilter()
    return "".join(filt.feed(c) for c in chunks) + filt.flush()


def test_streaming_filter_suppresses_markup_split_across_chunks():
    chunks = [
        "All ",
        "booked. ",
        "<func",
        "tion=send_conf",
        'irmation>{"job_id":',
        ' "x"}</fun',
        "ction>",
        " Bye.",
    ]
    assert _stream(chunks) == "All booked.  Bye."


def test_streaming_filter_is_transparent_to_normal_text():
    chunks = ["We can ", "be there ", "at 9am."]
    assert _stream(chunks) == "We can be there at 9am."


def test_streaming_filter_drops_an_unterminated_leak_at_end_of_stream():
    assert _stream(["Done. ", '<function=book_job>{"a": 1}']) == "Done. "


def test_lone_angle_bracket_is_not_swallowed():
    assert _stream(["that's < 5 minutes away"]) == "that's < 5 minutes away"


async def test_leaked_call_never_reaches_the_caller_and_still_executes(scripted, hotel):
    """End to end: the markup is silenced and the tool actually runs."""
    from app.tools.booking_tools import book_job, check_availability
    from tests.conftest import tool_config

    listing = await check_availability.ainvoke(
        {"service": "room-reservation"}, config=tool_config(hotel.tenant_id)
    )
    slot = listing.split("slot_start_iso=")[1].split(")")[0]
    booked = await book_job.ainvoke(
        {
            "service": "room-reservation",
            "slot_start_iso": slot,
            "customer_name": "Dana",
            "customer_phone": "5551112222",
        },
        config=tool_config(hotel.tenant_id),
    )
    job_id = booked.split("job_id=")[1].split()[0]

    scripted(
        ai(f'That is all booked. <function=send_confirmation>{{"job_id": "{job_id}"}}</function>'),
        ai("Text is on its way."),
    )

    events = [
        event
        async for event in stream_turn(
            text="all set?", tenant_id=hotel.tenant_id, session_id="leak"
        )
    ]

    spoken = "".join(e.text for e in events if e.is_spoken)
    assert "<function" not in spoken
    assert "job_id" not in spoken
    assert "That is all booked." in spoken

    assert [e.tool for e in events if e.type == "tool_start"] == ["send_confirmation"]
    assert len(get_store().list_messages(hotel.tenant_id)) == 1


class TestExtractUrls:
    def test_finds_a_url_mentioned_in_a_sentence(self):
        assert extract_urls("You can find that at https://example.com/careers.") == [
            "https://example.com/careers"
        ]

    def test_trailing_sentence_punctuation_is_not_captured(self):
        for text, expected in [
            ("See https://example.com.", "https://example.com"),
            ("Check (https://example.com)", "https://example.com"),
            ('Quote: "https://example.com"', "https://example.com"),
            ("List: https://example.com, https://other.com.", None),
        ]:
            if expected is not None:
                assert extract_urls(text) == [expected]

    def test_multiple_distinct_urls_in_order(self):
        text = "Try https://a.example.com or https://b.example.com instead."
        assert extract_urls(text) == ["https://a.example.com", "https://b.example.com"]

    def test_a_repeated_url_is_deduplicated(self):
        text = "Go to https://example.com now. Again, https://example.com."
        assert extract_urls(text) == ["https://example.com"]

    def test_no_urls_returns_an_empty_list(self):
        assert extract_urls("There is nothing to link here.") == []

    def test_http_and_https_both_match(self):
        assert extract_urls("http://example.com and https://example.com") == [
            "http://example.com",
            "https://example.com",
        ]
