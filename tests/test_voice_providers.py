"""The STT/TTS factory, and the contract the fakes have to satisfy.

The fakes are not test scaffolding here — `VOICE_STT_PROVIDER=fake` is the
*default*, so this is production code on any box without a Deepgram key.
"""

from __future__ import annotations

import pytest

from app.config import reset_settings_cache
from app.tenancy.models import VoiceSettings
from app.voice.audio import duration_ms
from app.voice.providers import (
    VoiceProviderError,
    get_stt,
    get_tts,
    reset_voice_overrides,
    set_voice_overrides,
)
from app.voice.stt.fake import DEFAULT_UTTERANCE, FakeSpeechToText
from app.voice.tts.fake import FakeTextToSpeech


def speak(tts, text: str, *, speed: float = 1.0):
    return tts.synthesize(text, voice=VoiceSettings(speed=speed), sample_rate=24_000)


def settings_with(monkeypatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()


def test_the_default_is_a_working_fake_pair(hotel):
    """A box with no keys still gets a tester that runs the whole state
    machine, the real brain and real metrics — only the words are canned."""
    assert get_stt(hotel).name == "fake"
    assert get_tts(hotel).name == "fake"


def test_overrides_win_over_settings(hotel, monkeypatch):
    settings_with(monkeypatch, VOICE_STT_PROVIDER="deepgram", DEEPGRAM_API_KEY="x")
    stt = FakeSpeechToText()
    set_voice_overrides(stt=stt, tts=FakeTextToSpeech())
    try:
        assert get_stt(hotel) is stt
    finally:
        reset_voice_overrides()


def test_deepgram_without_a_key_fails_loudly_rather_than_degrading(hotel, monkeypatch):
    """Silence that looks like a working tester is worse than a refused
    socket: an operator would read it as the *bot* being broken."""
    settings_with(monkeypatch, VOICE_STT_PROVIDER="deepgram")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    reset_settings_cache()

    with pytest.raises(VoiceProviderError, match="DEEPGRAM_API_KEY"):
        get_stt(hotel)


def test_cartesia_without_a_key_fails_loudly(hotel, monkeypatch):
    settings_with(monkeypatch, VOICE_TTS_PROVIDER="cartesia")
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    reset_settings_cache()

    with pytest.raises(VoiceProviderError, match="CARTESIA_API_KEY"):
        get_tts(hotel)


def test_a_configured_provider_is_actually_built(hotel, monkeypatch):
    settings_with(
        monkeypatch,
        VOICE_STT_PROVIDER="deepgram",
        DEEPGRAM_API_KEY="dg",
        VOICE_TTS_PROVIDER="cartesia",
        CARTESIA_API_KEY="ct",
    )
    assert get_stt(hotel).name == "deepgram"
    assert get_tts(hotel).name == "cartesia"


async def test_the_fake_stt_emits_an_interim_before_its_final():
    """Interim results are the half a real provider is otherwise the only
    source of — exercising that path offline is the point."""
    stt = FakeSpeechToText(["do you have parking"])
    session = await stt.open(sample_rate=16_000, language="en")
    await session.finish()

    results = [r async for r in session.results()]
    assert [r.is_final for r in results] == [False, True]
    assert results[-1].text == "do you have parking"


async def test_the_fake_stt_consumes_its_script_one_utterance_at_a_time():
    stt = FakeSpeechToText(["first", "second"])
    heard = []
    for _ in range(3):
        session = await stt.open(sample_rate=16_000, language="en")
        await session.finish()
        heard.append([r async for r in session.results()][-1].text)

    # ...and falls back to something a real tester would actually say once
    # the script runs out, not a debug string.
    assert heard == ["first", "second", DEFAULT_UTTERANCE]


async def test_the_fake_tts_returns_silence_of_a_plausible_length():
    """Length matters: a fake returning one empty chunk would hide the bug
    this orchestrator is most likely to have — emitting a turn's audio far
    faster than real time."""
    tts = FakeTextToSpeech()
    pcm = b"".join([c async for c in speak(tts, "one two three")])

    assert 1000 <= duration_ms(pcm, 24_000) <= 1600


async def test_the_fake_tts_speeds_up_with_the_tenants_speed_setting():
    tts = FakeTextToSpeech()
    normal = b"".join([c async for c in speak(tts, "one two three")])
    fast = b"".join([c async for c in speak(tts, "one two three", speed=2.0)])

    assert len(fast) < len(normal)
