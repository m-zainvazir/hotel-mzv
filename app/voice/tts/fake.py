"""Silent TTS — silence of the *right length*.

The length matters. A fake returning one empty chunk would make every timing
assertion meaningless and would hide the bug this orchestrator is most
likely to have: emitting a turn's audio far faster than real time, so the
browser's buffer and the server's idea of "speaking" disagree. Silence sized
from the text keeps offline tests honest about duration without making them
slow — nothing here actually sleeps.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.tenancy.models import VoiceSettings
from app.voice.audio import silence

#: Rough conversational pace. ~150 wpm is about 400ms per word at speed 1.0.
MS_PER_WORD = 400.0
#: Every synthesis yields at least this, so even a one-word chunk produces a
#: measurable first-audio moment.
MIN_MS = 120.0


class FakeTextToSpeech:
    name = "fake"

    def __init__(self, chunk_ms: float = 200.0) -> None:
        self.chunk_ms = chunk_ms
        #: Everything asked for, in order — what a test asserts on, instead
        #: of trying to read meaning out of silence.
        self.spoken: list[str] = []

    async def synthesize(
        self, text: str, *, voice: VoiceSettings, sample_rate: int
    ) -> AsyncIterator[bytes]:
        self.spoken.append(text)
        words = max(1, len(text.split()))
        total = max(MIN_MS, words * MS_PER_WORD / max(voice.speed, 0.1))
        remaining = total
        while remaining > 0:
            step = min(self.chunk_ms, remaining)
            remaining -= step
            yield silence(step, sample_rate)
