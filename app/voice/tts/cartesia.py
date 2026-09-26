"""Cartesia TTS over plain HTTP streaming.

`POST /tts/bytes` with `container: "raw"`, not their WebSocket endpoint —
deliberately, and the reason is the whole of D2: raw `pcm_s16le` comes back
as the response body, httpx streams it chunk by chunk, and this repo already
has httpx everywhere (the "raw httpx, no SDKs" precedent from plan §10). A
WebSocket here would add a dependency and a second connection lifecycle to
own, in exchange for a saving that doesn't exist at one request per sentence.

Each synthesis is its own request, because each *sentence* is its own
request — that is what makes first audio land on the first sentence rather
than the last, and it's why the client below is the shared, connection-pooled
one: a cold TLS handshake per sentence would eat the §13 budget outright.

⚠️ Shape unverified against a live account. `tests/test_voice_cartesia.py`
proves what we *send* and how we read what comes back; only a real call can
prove Cartesia agrees. Same honesty caveat Phase 9 Part A's Cal.com MCP tool
schemas carried until a live booking closed it. A mismatch surfaces as a loud
`TtsError` on the first sentence, never as silently wrong audio.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from app.config import get_settings
from app.tenancy.models import VoiceSettings
from app.tools.http_client import shared_async_client
from app.voice.tts.base import TtsError

logger = logging.getLogger(__name__)

_PATH = "/tts/bytes"


class CartesiaTextToSpeech:
    name = "cartesia"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    def _resolve_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        settings = get_settings()
        if not settings.cartesia_api_key:
            raise TtsError("Cartesia is not configured (CARTESIA_API_KEY unset)")
        return shared_async_client(
            # The key carries a credential fingerprint, so a test that swaps
            # keys never reuses a client built with the old one.
            f"cartesia-tts:{settings.cartesia_api_key[:8]}",
            base_url=settings.cartesia_api_base,
            headers={
                "Authorization": f"Bearer {settings.cartesia_api_key}",
                "Cartesia-Version": settings.cartesia_api_version,
                "Content-Type": "application/json",
            },
            # A sentence, not a file upload: the clone timeout would be
            # absurd here, and a slow sentence should fail fast enough that
            # the next one still gets spoken.
            timeout=15.0,
        )

    def _payload(self, text: str, voice: VoiceSettings, sample_rate: int) -> dict:
        settings = get_settings()
        voice_id = voice.voice_id or settings.cartesia_default_voice_id
        if not voice_id:
            raise TtsError(
                "no Cartesia voice id — set the tenant's voice.voice_id or "
                "CARTESIA_DEFAULT_VOICE_ID"
            )
        return {
            "model_id": voice.model,
            "transcript": text,
            "voice": {"mode": "id", "id": voice_id},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": sample_rate,
            },
            "language": "en",
            "speed": voice.speed,
        }

    async def synthesize(
        self, text: str, *, voice: VoiceSettings, sample_rate: int
    ) -> AsyncIterator[bytes]:
        client = self._resolve_client()
        payload = self._payload(text, voice, sample_rate)
        try:
            async with client.stream("POST", _PATH, json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:300]
                    raise TtsError(f"Cartesia TTS failed: {response.status_code} {body}")
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            # `type(exc).__name__` alongside the message, always: `str()` of
            # an httpx timeout is the empty string, and a log line naming
            # nothing is how the tenant-snapshot bug took a live repro to
            # diagnose.
            raise TtsError(f"Cartesia TTS unreachable: {type(exc).__name__}: {exc}") from exc
