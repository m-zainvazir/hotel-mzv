"""Scripted STT — the default provider, and the one every offline test uses.

Deliberately the *default* (`VOICE_STT_PROVIDER=fake`) rather than a
test-only fixture: a box with no Deepgram key still gets a fully working
tester to click through, with real state transitions, real metrics and the
real brain. Only the words are canned.

It also answers the question a mock can't: the whole orchestration path is
exercised on every CI run, not just on the machine that happens to hold a
paid key.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from app.voice.audio import BYTES_PER_SAMPLE
from app.voice.stt.base import Transcript

#: What the fake "hears" when nothing is scripted. Phrased as something a
#: real tester would actually say, so a click-through with no key still
#: produces a sensible conversation rather than a debug string.
DEFAULT_UTTERANCE = "What time do you open?"


class FakeSttSession:
    def __init__(self, script: list[str], *, sample_rate: int) -> None:
        self._script = script
        self._sample_rate = sample_rate
        self._bytes = 0
        self._queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._finished = False

    @property
    def audio_bytes(self) -> int:
        """How much audio was actually pushed — the one thing a fake can
        honestly assert about the transport."""
        return self._bytes

    def seconds_received(self) -> float:
        return self._bytes / (self._sample_rate * BYTES_PER_SAMPLE)

    async def send_audio(self, pcm: bytes) -> None:
        self._bytes += len(pcm)

    async def utter(self, text: str) -> None:
        """Push one complete utterance, endpoint and all, into a session that
        stays open.

        The real provider does this off the audio itself; the fake has no
        audio to read, so a test says when. Without this there is no way to
        exercise a *second* utterance on one open mic — which is the whole
        behaviour an always-on mic adds.
        """
        head = text.split(" ")[0]
        if head and head != text:
            await self._queue.put(Transcript(head, is_final=False))
        await self._queue.put(Transcript(text, is_final=True, speech_final=True))

    async def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        text = self._script.pop(0) if self._script else DEFAULT_UTTERANCE
        # An interim first, so the UI path that renders partial results is
        # exercised offline too — it's the half a real provider is otherwise
        # the only source of.
        head = text.split(" ")[0]
        if head and head != text:
            await self._queue.put(Transcript(head, is_final=False))
        # `speech_final` too: the fake has no audio to endpoint, but a real
        # provider marks the end of an utterance here, and emitting it keeps
        # both providers on the one code path that turns an endpoint into a
        # turn — rather than the fake quietly exercising a different one.
        await self._queue.put(Transcript(text, is_final=True, speech_final=True))
        await self._queue.put(None)

    async def results(self) -> AsyncIterator[Transcript]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def aclose(self) -> None:
        if not self._finished:
            self._finished = True
            await self._queue.put(None)


class FakeSpeechToText:
    """`script` is consumed one entry per utterance, in order."""

    name = "fake"

    def __init__(self, script: list[str] | None = None) -> None:
        self.script = list(script or [])
        #: Every session opened, so a test can assert on audio actually sent.
        self.sessions: list[FakeSttSession] = []

    async def open(self, *, sample_rate: int, language: str) -> FakeSttSession:
        del language
        session = FakeSttSession(self.script, sample_rate=sample_rate)
        self.sessions.append(session)
        return session
