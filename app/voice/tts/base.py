"""The TTS seam.

An async iterator of PCM chunks rather than one `bytes` return, and that is
the whole §13 argument in one type signature: the first chunk must reach the
browser while the provider is still synthesizing the rest of the sentence.
A provider that can only return complete audio still satisfies this — it
yields once — but the seam never *forces* that on the ones that can stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.tenancy.models import VoiceSettings


class TtsError(RuntimeError):
    """The provider failed. Caught per chunk: a sentence that can't be spoken
    costs that sentence's audio, never the turn's remaining text."""


class TextToSpeech(Protocol):
    #: Named for `/health` and the `ready` handshake, never branched on.
    name: str

    def synthesize(
        self, text: str, *, voice: VoiceSettings, sample_rate: int
    ) -> AsyncIterator[bytes]:
        """`linear16` mono at `sample_rate`, in whatever chunk sizes the
        provider produces — `app/voice/audio.py::iter_frames` does the
        wire-sizing, so a provider never has to care."""
        ...
