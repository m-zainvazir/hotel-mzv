"""Latency measurement — the deliverable, not a nice-to-have.

A tester that works but can't say whether we hold the §13 budget has
delivered the demo and skipped the answer (plans/phase9.3.md's "Done when").
"""

from __future__ import annotations

import asyncio

from app.voice.protocol import ClientMessage
from app.voice.session import TurnMetrics, percentile, summarise
from app.voice.stt.fake import FakeSpeechToText
from app.voice.tts.fake import FakeTextToSpeech
from tests.conftest import ai
from tests.test_voice_turn import build


async def test_a_turn_reports_every_stage(hotel, scripted):
    scripted(ai("We open at seven."))
    session, recorder = build(hotel)
    await session.start()

    await session.handle(ClientMessage("text", "when do you open?"))
    await session.join_turn()

    reported = recorder.of_type("metrics")
    assert len(reported) == 1
    measurement = reported[0]
    assert measurement["turn"] == 1
    for stage in ("stt_final_ms", "first_token_ms", "first_audio_ms", "total_ms"):
        assert measurement[stage] is not None, f"{stage} was never measured"
    # First audio can't precede the first token that produced it.
    assert measurement["first_audio_ms"] >= measurement["first_token_ms"]


async def test_the_clock_starts_at_end_of_speech_not_at_socket_open(hotel, scripted):
    """§13 measures end-of-speech → first audio. Starting the clock at the
    handshake would quietly include however long the speaker talked for, and
    make every number incomparable with Vapi's own."""
    scripted(ai("Sure."))
    session, recorder = build(hotel, stt=FakeSpeechToText(["hello"]))
    await session.start()

    await session.handle(ClientMessage("start_utterance"))
    await asyncio.sleep(0.05)  # "talking"
    await session.feed_audio(bytes(640))
    await asyncio.sleep(0.05)
    await session.handle(ClientMessage("end_utterance"))
    await session.join_turn()

    measured = recorder.of_type("metrics")[0]
    # The 100ms spent holding the mic must not be in the number.
    assert measured["first_audio_ms"] < 100


async def test_a_failed_turn_still_reports_metrics(hotel, scripted):
    """Metrics come from the `finally`: a turn that dies is exactly the turn
    whose timing you want to see."""

    class BrokenTts:
        name = "broken"

        async def synthesize(self, text, *, voice, sample_rate):
            raise RuntimeError("no synthesizer today")
            yield b""  # pragma: no cover - unreachable, satisfies the generator

    scripted(ai("Hello there."))
    session, recorder = build(hotel, tts=BrokenTts())
    await session.start()

    await session.handle(ClientMessage("text", "hi"))
    await session.join_turn()

    assert recorder.of_type("metrics")
    assert recorder.of_type("error")
    # ...and the text still reached the client even though the audio didn't.
    assert "Hello there." in recorder.spoken_text()


async def test_the_session_summary_is_computed_across_turns(hotel, scripted):
    scripted(ai("One."), ai("Two."))
    session, _ = build(hotel, tts=FakeTextToSpeech())
    await session.start()

    await session.handle(ClientMessage("text", "first"))
    await session.join_turn()
    await session.handle(ClientMessage("text", "second"))
    await session.join_turn()

    summary = summarise(session.metrics)
    assert summary["turns"] == 2
    assert summary["first_audio_p50_ms"] is not None
    assert summary["first_audio_p95_ms"] >= summary["first_audio_p50_ms"]


def test_the_median_of_two_samples_is_the_lower_one():
    """Python's `round` is banker's rounding, which put the p50 of two
    samples on the *worse* one — the wrong direction to be wrong in for the
    single measurement this phase exists to produce."""
    assert percentile([500.0, 700.0], 0.50) == 500.0
    assert percentile([500.0, 700.0], 0.95) == 700.0


def test_percentiles_of_an_empty_session_are_none_not_zero():
    """Zero would read as "instant" on a dashboard. Nothing measured is not
    the same as nothing taken."""
    assert percentile([], 0.5) is None
    assert summarise([]) == {
        "turns": 0,
        "first_audio_p50_ms": None,
        "first_audio_p95_ms": None,
    }


def test_a_turn_that_never_spoke_is_excluded_from_the_percentiles():
    metrics = [
        TurnMetrics(1, first_audio_ms=600.0),
        TurnMetrics(2, first_audio_ms=None),  # cancelled before any audio
    ]
    assert summarise(metrics)["first_audio_p50_ms"] == 600.0


async def test_a_turn_that_speaks_only_its_filler_leaves_a_log_line(hotel, scripted, caplog):
    """Reported live: the bot said "checking availability now" and then went
    quiet. The error that caused it was overwritten in the UI by the metrics
    line, so it left no trace anywhere. Now it names itself in the log."""
    import logging

    scripted(
        ai("", [{"name": "check_availability", "args": {"service": "room-reservation"}}]),
        ai(""),  # the model returns nothing after the tool
    )
    session, _ = build(hotel)
    await session.start()

    with caplog.at_level(logging.WARNING, logger="app.voice.session"):
        await session.handle(ClientMessage("text", "what's the earliest possible?"))
        await session.join_turn()

    assert any("spoke only an acknowledgement" in r.message for r in caplog.records)
    assert any("earliest possible" in str(r.args) for r in caplog.records)


async def test_the_continuation_pause_counts_against_the_latency_budget(
    hotel, monkeypatch, scripted
):
    """§13 measures end-of-speech → first audio. Starting the clock when the
    turn *begins* hid the continuation pause entirely: the reported number
    was shorter than the silence the listener sat through, and
    `stt_final_ms` read 0ms on every single turn."""
    monkeypatch.setenv("VOICE_CONTINUATION_PAUSE_SECONDS", "0.2")
    from app.config import reset_settings_cache

    reset_settings_cache()
    scripted(ai("Seven."))
    stt = FakeSpeechToText()
    session, recorder = build(hotel, stt=stt)
    await session.start()

    await session.handle(ClientMessage("mic_on"))
    await stt.sessions[0].utter("what time do you open?")
    await asyncio.sleep(0.5)
    await session.join_turn()

    measured = recorder.of_type("metrics")[0]
    # The 200ms pause is inside the number, not hidden behind it.
    assert measured["stt_final_ms"] >= 150, measured
    assert measured["first_audio_ms"] >= measured["stt_final_ms"]
