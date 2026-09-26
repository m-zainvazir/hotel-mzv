"""The Cartesia TTS adapter's request shape and streaming behaviour.

Our half only — see `tests/test_voice_deepgram.py`'s header for why that
distinction is worth stating rather than assuming.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import reset_settings_cache
from app.tenancy.models import VoiceSettings
from app.voice.tts.base import TtsError
from app.voice.tts.cartesia import CartesiaTextToSpeech


@pytest.fixture(autouse=True)
def cartesia_key(monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "ct-test-key")
    monkeypatch.setenv("CARTESIA_DEFAULT_VOICE_ID", "default-voice")
    reset_settings_cache()


def client_returning(chunks: list[bytes], *, status: int = 200, captured: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["body"] = json.loads(request.content)
            captured["headers"] = dict(request.headers)
            captured["url"] = str(request.url)
        if status >= 400:
            return httpx.Response(status, text="nope")
        return httpx.Response(status, content=b"".join(chunks))

    return httpx.AsyncClient(
        base_url="https://api.cartesia.ai", transport=httpx.MockTransport(handler)
    )


async def synthesize(tts, text="Hello there.", voice=None, sample_rate=24_000) -> list[bytes]:
    return [
        chunk
        async for chunk in tts.synthesize(
            text, voice=voice or VoiceSettings(), sample_rate=sample_rate
        )
    ]


async def test_the_request_asks_for_raw_pcm_at_our_output_rate():
    """The one field that must never drift: `app/voice/audio.py` promises the
    browser 24kHz raw `pcm_s16le`, and a container (wav/mp3) here would put a
    header in the middle of the audio stream."""
    captured: dict = {}
    tts = CartesiaTextToSpeech(client=client_returning([b"\x00" * 96], captured=captured))

    await synthesize(tts)

    assert captured["body"]["output_format"] == {
        "container": "raw",
        "encoding": "pcm_s16le",
        "sample_rate": 24_000,
    }
    assert captured["body"]["transcript"] == "Hello there."


async def test_the_tenants_own_voice_and_speed_are_honoured():
    captured: dict = {}
    tts = CartesiaTextToSpeech(client=client_returning([b"\x00" * 96], captured=captured))

    await synthesize(tts, voice=VoiceSettings(voice_id="cloned-abc", speed=1.2, model="sonic-3"))

    assert captured["body"]["voice"] == {"mode": "id", "id": "cloned-abc"}
    assert captured["body"]["speed"] == 1.2
    assert captured["body"]["model_id"] == "sonic-3"


async def test_a_tenant_without_a_voice_id_falls_back_to_the_deployment_default():
    captured: dict = {}
    tts = CartesiaTextToSpeech(client=client_returning([b"\x00" * 96], captured=captured))

    await synthesize(tts)

    assert captured["body"]["voice"]["id"] == "default-voice"


async def test_no_voice_id_anywhere_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("CARTESIA_DEFAULT_VOICE_ID", raising=False)
    reset_settings_cache()
    tts = CartesiaTextToSpeech(client=client_returning([b""]))

    with pytest.raises(TtsError, match="voice id"):
        await synthesize(tts)


async def test_audio_is_yielded_as_it_arrives_not_buffered_whole():
    """The seam's entire purpose: the first chunk must be able to reach the
    browser while the rest is still being synthesized."""
    tts = CartesiaTextToSpeech(client=client_returning([b"\x01" * 48, b"\x02" * 48]))

    chunks = await synthesize(tts)

    assert b"".join(chunks) == b"\x01" * 48 + b"\x02" * 48


async def test_an_http_error_is_a_tts_error_carrying_the_status():
    tts = CartesiaTextToSpeech(client=client_returning([], status=402))

    with pytest.raises(TtsError, match="402"):
        await synthesize(tts)


async def test_an_unreachable_provider_names_the_exception_type():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("")

    tts = CartesiaTextToSpeech(
        client=httpx.AsyncClient(
            base_url="https://api.cartesia.ai", transport=httpx.MockTransport(handler)
        )
    )

    # `str()` of an httpx timeout is the empty string — without the type name
    # this log line would say "Cartesia TTS unreachable: " and name nothing.
    with pytest.raises(TtsError, match="ConnectTimeout"):
        await synthesize(tts)


async def test_a_missing_api_key_is_refused_before_any_request(monkeypatch):
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    reset_settings_cache()

    with pytest.raises(TtsError, match="CARTESIA_API_KEY"):
        await synthesize(CartesiaTextToSpeech())
