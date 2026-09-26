"""The STT seam.

Shaped as an explicit *session* object rather than
`transcribe(audio_stream) -> transcript_stream`, because a real streaming STT
socket sends and receives concurrently: audio goes up while interim results
come down, and the "I'm done talking" signal is a message on the same socket,
not the end of an iterator. An async-generator-in/async-generator-out shape
reads nicer and cannot express that without a queue behind it anyway.

Mirrors `app/tools/booking/base.py`: a Protocol plus a factory in
`app/voice/providers.py`, so no caller ever imports a vendor module.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Transcript:
    """One STT result.

    `is_final` marks a stable segment; everything else is an interim the UI
    may show and the brain must never see. `speech_final` is the stronger
    claim — *the speaker has stopped* — and it is what ends a turn on an
    open mic, so a listener never has to press anything to send. A provider
    that can't detect endpoints simply never sets it, and the mic switch
    falls back to flushing whatever it has when it closes.
    """

    text: str
    is_final: bool = False
    speech_final: bool = False
    #: The provider's voice-activity detector heard sound start. Carries no
    #: words — it arrives long before any — which is exactly why it is worth
    #: having: it is the earliest possible evidence that a pause is not the
    #: end of a sentence.
    speech_started: bool = False


class SttError(RuntimeError):
    """The provider failed. Always caught by the session and turned into a
    `protocol.error` for this turn — never a closed socket, since the next
    utterance may well work."""


@runtime_checkable
class SttSession(Protocol):
    """One utterance. Opened when the speaker takes the floor, closed when
    the turn ends — never held for the life of the socket, so an idle
    connected tab bills nothing (plans/phase9.3.md, "Cost")."""

    async def send_audio(self, pcm: bytes) -> None:
        """Push one frame of `linear16` at the rate this session was opened with."""
        ...

    async def finish(self) -> None:
        """The speaker stopped. Ask the provider to flush its final result."""
        ...

    def results(self) -> AsyncIterator[Transcript]:
        """Transcripts as they arrive, ending after the final one."""
        ...

    async def aclose(self) -> None:
        """Release the connection. Must be safe to call twice, and must not
        raise — it runs in the turn's `finally`."""
        ...


class SpeechToText(Protocol):
    #: Named for `/health` and the `ready` handshake, never branched on.
    name: str

    async def open(self, *, sample_rate: int, language: str) -> SttSession: ...
