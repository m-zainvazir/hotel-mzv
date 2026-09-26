"""The Deepgram adapter's framing — what we send, and how we read what comes back.

These prove *our* half only. Nobody has a Deepgram key yet
(plans/phase9.3.md's blocker table), so the vendor's half is unverified and
these tests must not be read as proof that Deepgram agrees — the same caveat
Phase 9 Part A's Cal.com MCP schemas carried until a live booking closed it.
"""

from __future__ import annotations

import json

import pytest

from app.config import reset_settings_cache
from app.voice.stt.base import SttError
from app.voice.stt.deepgram import DeepgramSpeechToText, _connect_url, _transcript_text


@pytest.fixture(autouse=True)
def deepgram_key(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-key")
    reset_settings_cache()


class FakeConnection:
    """Stands in for a `websockets` client connection."""

    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[object] = []
        self.closed = False

    async def send(self, payload) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        async def _iterate():
            for frame in self.frames:
                yield frame

        return _iterate()


def results_frame(text: str, *, is_final: bool) -> str:
    return json.dumps(
        {
            "type": "Results",
            "is_final": is_final,
            "channel": {"alternatives": [{"transcript": text}]},
        }
    )


def connector(connection: FakeConnection):
    async def _connect(url, **kwargs):
        connection.url = url
        connection.kwargs = kwargs
        return connection

    return _connect


async def test_the_connect_url_declares_our_actual_audio_format():
    """A mismatch here is silent: Deepgram would happily transcribe 16kHz
    PCM as if it were 8kHz and return confident nonsense."""
    url = _connect_url(sample_rate=16_000, language="en")
    assert "encoding=linear16" in url
    assert "sample_rate=16000" in url
    assert "channels=1" in url
    # The chunker splits on sentence boundaries, so a transcript with no
    # punctuation would give it nothing to split on.
    assert "smart_format=true" in url
    assert "interim_results=true" in url


async def test_the_api_key_travels_as_a_header_not_a_query_parameter():
    """Deepgram's own scheme is `Token`, not `Bearer`. A WS *client* can set
    headers (only browsers can't), so the key never needs to ride in a URL
    the way the query-auth MCP servers force."""
    connection = FakeConnection([])
    stt = DeepgramSpeechToText(connect=connector(connection))
    await stt.open(sample_rate=16_000, language="en")

    assert connection.kwargs["additional_headers"]["Authorization"] == "Token dg-test-key"
    assert "dg-test-key" not in connection.url


async def test_finals_and_interims_are_distinguished():
    connection = FakeConnection(
        [
            results_frame("do you", is_final=False),
            results_frame("do you have parking", is_final=True),
            json.dumps({"type": "Metadata"}),
            results_frame("never read", is_final=True),
        ]
    )
    stt = DeepgramSpeechToText(connect=connector(connection))
    session = await stt.open(sample_rate=16_000, language="en")

    collected = [r async for r in session.results()]
    assert [(r.text, r.is_final) for r in collected] == [
        ("do you", False),
        ("do you have parking", True),
    ]
    # `Metadata` ends the stream — anything after it is Deepgram's, not ours.
    assert len(collected) == 2


async def test_finish_sends_close_stream_so_deepgram_flushes_its_last_result():
    connection = FakeConnection([])
    stt = DeepgramSpeechToText(connect=connector(connection))
    session = await stt.open(sample_rate=16_000, language="en")

    await session.send_audio(b"\x00\x01")
    await session.finish()

    assert connection.sent[0] == b"\x00\x01"
    assert json.loads(connection.sent[1]) == {"type": "CloseStream"}


async def test_audio_after_close_is_dropped_rather_than_raising():
    connection = FakeConnection([])
    stt = DeepgramSpeechToText(connect=connector(connection))
    session = await stt.open(sample_rate=16_000, language="en")

    await session.aclose()
    await session.send_audio(b"\x00\x01")

    assert connection.sent == []
    assert connection.closed


async def test_a_missing_key_is_a_clear_error_not_a_connection_attempt(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    reset_settings_cache()
    with pytest.raises(SttError, match="DEEPGRAM_API_KEY"):
        await DeepgramSpeechToText().open(sample_rate=16_000, language="en")


async def test_an_unreachable_provider_names_the_exception_type():
    """`str()` of an httpx/websockets timeout is often empty — a log line
    naming nothing is what made the tenant-snapshot bug need a live repro."""

    async def _explode(url, **kwargs):
        raise TimeoutError()

    with pytest.raises(SttError, match="TimeoutError"):
        await DeepgramSpeechToText(connect=_explode).open(sample_rate=16_000, language="en")


def test_a_frame_with_no_alternatives_is_not_a_crash():
    assert _transcript_text({"channel": {}}) == ""
    assert _transcript_text({}) == ""
    assert _transcript_text({"channel": {"alternatives": [{}]}}) == ""
