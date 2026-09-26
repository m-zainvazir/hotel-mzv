"""Deepgram streaming STT over WebSocket.

The one place in this phase that genuinely needs a WebSocket *client*: audio
goes up while interim results come down, and "I've stopped talking" is a
message on the same socket rather than the end of a stream. TTS gets away
with plain HTTP (see `app/voice/tts/cartesia.py`); this can't.

`websockets` is imported lazily inside the constructor, the
`app/mcp/client.py` pattern: a missing optional dependency must cost voice,
never the app. It arrives transitively with `uvicorn[standard]` today, which
is exactly why it's declared explicitly in the `voice` extra rather than
relied on — a transitive dependency that disappears in a future uvicorn
release would otherwise break this at runtime, on a box where it had always
worked.

⚠️ Shape unverified against a live account — nobody has a Deepgram key yet
(plans/phase9.3.md's blocker table). `tests/test_voice_deepgram.py` proves
the query we build and how we read their frames; only a real key can prove
Deepgram agrees. A mismatch shows up as an empty transcript and a logged
warning, never as a wrong answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

from app.config import get_settings
from app.voice.stt.base import SttError, Transcript

logger = logging.getLogger(__name__)


def _connect_url(*, sample_rate: int, language: str) -> str:
    settings = get_settings()
    query = urlencode(
        {
            "model": settings.deepgram_model,
            "language": language,
            "encoding": "linear16",
            "sample_rate": sample_rate,
            "channels": 1,
            # Interim results are what let the tester show words as they're
            # spoken; the brain only ever sees finals.
            "interim_results": "true",
            # Punctuation matters more here than anywhere else in this repo:
            # the reply chunker splits on sentence boundaries, and a
            # transcript with no punctuation gives it nothing to split on.
            "smart_format": "true",
            # Endpointing is what lets an open mic work without a send
            # button: after this much silence Deepgram marks the result
            # `speech_final`, and the session turns that into a turn.
            "endpointing": settings.deepgram_endpointing_ms,
            # The backstop for the case endpointing alone misses — a speaker
            # who trails off mid-phrase never gets a `speech_final`, so
            # Deepgram sends a standalone `UtteranceEnd` after this much
            # silence instead. Requires `interim_results=true`, above.
            "utterance_end_ms": settings.deepgram_utterance_end_ms,
            "vad_events": "true",
        }
    )
    return f"{settings.deepgram_api_base}?{query}"


class DeepgramSttSession:
    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._closed = False

    async def send_audio(self, pcm: bytes) -> None:
        if self._closed:
            return
        await self._connection.send(pcm)

    async def finish(self) -> None:
        """Deepgram flushes and sends its last `Results` after this, then
        `Metadata` — which is what `results()` stops on."""
        if self._closed:
            return
        try:
            await self._connection.send(json.dumps({"type": "CloseStream"}))
        except Exception as exc:  # the socket may already be gone
            logger.debug("deepgram CloseStream failed: %s", exc)

    async def results(self) -> AsyncIterator[Transcript]:
        try:
            async for raw in self._connection:
                if isinstance(raw, bytes):
                    continue  # Deepgram sends text frames; ignore anything else
                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                kind = payload.get("type")
                if kind == "Metadata":
                    return  # the stream is finished
                if kind == "SpeechStarted":
                    # `vad_events=true` gives this the moment sound begins,
                    # about a second before the first word is transcribed.
                    # Continuation mode needs that head start: waiting for a
                    # word meant the "have they finished?" countdown had
                    # already fired and split the sentence.
                    yield Transcript("", speech_started=True)
                    continue
                if kind == "UtteranceEnd":
                    # A bare endpoint with no words of its own: Deepgram saw
                    # the speaker stop without ever marking a `speech_final`.
                    # Carries no text, so it flushes whatever finals already
                    # arrived rather than adding to them.
                    yield Transcript("", is_final=False, speech_final=True)
                    continue
                if kind and kind != "Results":
                    continue
                text = _transcript_text(payload)
                speech_final = bool(payload.get("speech_final"))
                if not text and not speech_final:
                    continue
                yield Transcript(
                    text,
                    is_final=bool(payload.get("is_final")),
                    speech_final=speech_final,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise SttError(f"deepgram stream failed: {type(exc).__name__}: {exc}") from exc

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._connection.close()
        except Exception as exc:
            logger.debug("deepgram close failed: %s", exc)


def _transcript_text(payload: dict[str, Any]) -> str:
    alternatives = (payload.get("channel") or {}).get("alternatives") or []
    if not alternatives:
        return ""
    return str(alternatives[0].get("transcript") or "").strip()


class DeepgramSpeechToText:
    name = "deepgram"

    def __init__(self, connect=None) -> None:
        #: Injectable for tests — the same shape `CalcomBookingProvider` and
        #: `SupabaseStore` use for their `client=` overrides.
        self._connect = connect

    async def open(self, *, sample_rate: int, language: str) -> DeepgramSttSession:
        settings = get_settings()
        if not settings.deepgram_api_key:
            raise SttError("Deepgram is not configured (DEEPGRAM_API_KEY unset)")

        connect = self._connect
        if connect is None:
            try:
                from websockets.asyncio.client import connect as ws_connect
            except ImportError as exc:
                raise SttError(
                    "deepgram STT needs the `voice` extra: pip install -e '.[voice]'"
                ) from exc
            connect = ws_connect

        url = _connect_url(sample_rate=sample_rate, language=language)
        try:
            connection = await connect(
                url,
                # Deepgram's own scheme — `Token`, not `Bearer`. A WS client
                # *can* set headers (only browsers can't), which is why this
                # key never rides in the query string the way the URL-auth
                # MCP servers force.
                additional_headers={"Authorization": f"Token {settings.deepgram_api_key}"},
            )
        except Exception as exc:
            raise SttError(f"deepgram unreachable: {type(exc).__name__}: {exc}") from exc
        return DeepgramSttSession(connection)
