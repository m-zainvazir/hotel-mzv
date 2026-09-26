"""The turn orchestrator — STT → brain → TTS, and the state machine around it.

Everything that makes voice *voice* lives here; `app/channels/voice_live.py`
only moves bytes. That split is the reason this file is testable without a
WebSocket at all: it talks to a `Transport`, which the route satisfies and a
list-appending test double also satisfies.

Two rules inherited from the phone channel, both load-bearing:

* **Only `BrainEvent.is_spoken` becomes audio.** `tool_start`/`tool_result`
  are logged, never synthesized — CLAUDE.md's rule, and the consequence of
  breaking it is a caller hearing raw tool output read aloud.
* **Never wait for the full reply before speaking.** The reply is chunked on
  sentence boundaries and each chunk is synthesized as it completes, so first
  audio lands on the first sentence rather than the last. This is where the
  §13 budget is won or lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import sys
import uuid
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Protocol

from app.brain.runner import stream_turn
from app.brain.sanitize import _SENTENCE_END
from app.config import Settings, get_settings
from app.tenancy.models import TenantConfig
from app.voice import protocol
from app.voice.audio import INPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE, iter_frames
from app.voice.protocol import ClientMessage
from app.voice.providers import get_stt, get_tts
from app.voice.stt.base import SpeechToText, SttSession
from app.voice.tts.base import TextToSpeech

logger = logging.getLogger(__name__)

#: A clause boundary, used at most once per turn to get the *first* audio out
#: sooner (see `_take_chunks`).
_CLAUSE_END = re.compile(r"[,;:]\s|\s[—–]\s")
#: Only consider an early clause flush once the model has produced at least
#: this much without finishing a sentence — below it, the sentence is about
#: to land anyway and the seam costs more than the wait.
FIRST_CHUNK_MIN_CHARS = 40
#: ...and the piece actually flushed must be at least this long, or the
#: listener hears "Sure," alone followed by a gap. Lower than the trigger on
#: purpose: the point is to split a *long* opener, so the head is allowed to
#: be shorter than the buffer that justified splitting it.
MIN_CLAUSE_CHARS = 25


class Transport(Protocol):
    """What the orchestrator needs from a socket, and nothing more."""

    async def send_json(self, payload: dict[str, Any]) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


@dataclass
class TurnMetrics:
    """Times from `end_utterance` (or a typed message), in ms.

    The clock starts when the *speaker stops*, not when the socket opens —
    that's the §13 definition ("end-of-speech → first audio") and the only
    one comparable against Vapi's own numbers.
    """

    turn: int
    stt_final_ms: float | None = None
    first_token_ms: float | None = None
    first_audio_ms: float | None = None
    total_ms: float | None = None
    #: Cut short by a barge-in. Reported so a turn that stops mid-answer is
    #: never indistinguishable from a bot that simply ignored the question —
    #: which is exactly how it read in a real session.
    interrupted: bool = False

    def as_message(self) -> dict[str, Any]:
        return protocol.metrics(
            self.turn,
            stt_final_ms=_round(self.stt_final_ms),
            first_token_ms=_round(self.first_token_ms),
            first_audio_ms=_round(self.first_audio_ms),
            total_ms=_round(self.total_ms),
            interrupted=self.interrupted,
        )


@dataclass
class SessionLimits:
    max_turns: int
    max_seconds: float
    idle_seconds: float
    max_utterance_bytes: int
    stt_final_timeout: float

    @classmethod
    def from_settings(cls, settings: Settings) -> SessionLimits:
        return cls(
            max_turns=settings.voice_max_turns_per_session,
            max_seconds=settings.voice_max_session_seconds,
            idle_seconds=settings.voice_idle_timeout_seconds,
            max_utterance_bytes=settings.voice_max_utterance_bytes,
            stt_final_timeout=settings.voice_stt_final_timeout_seconds,
        )


class SessionClosed(Exception):
    """A cap was hit. Carries the reason the client is told before the close
    frame — a session that goes silent at its limit is indistinguishable from
    a crash, and these caps exist precisely to be hit.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _take_chunks(buffer: str, *, allow_clause_flush: bool) -> tuple[list[str], str]:
    """Split off whatever is ready to speak, returning `(chunks, remainder)`.

    Sentence boundaries come from `app/brain/sanitize.py::_SENTENCE_END` —
    deliberately the same regex `RepeatSuppressor` uses rather than a second
    splitter, because two that disagree is exactly how the repeat-suppression
    bugs happened.

    `allow_clause_flush` is true only until the first chunk of a turn has been
    spoken: a long opening sentence otherwise holds all audio back, and the
    first chunk is the only one whose latency a listener can perceive.
    """
    chunks: list[str] = []
    rest = buffer
    while True:
        match = _SENTENCE_END.search(rest)
        if match is None:
            break
        chunk = rest[: match.end()].strip()
        rest = rest[match.end() :]
        if chunk:
            chunks.append(chunk)

    if not chunks and allow_clause_flush and len(rest.strip()) >= FIRST_CHUNK_MIN_CHARS:
        # The *last* qualifying boundary, not the first: flushing at the
        # earliest comma turns "Sure, I can check the diary for you" into a
        # two-word chunk followed by a seam, which sounds worse than the
        # handful of milliseconds it saves. The head has to be worth
        # speaking on its own, hence `MIN_CLAUSE_CHARS`.
        for match in _CLAUSE_END.finditer(rest):
            head = rest[: match.end()].strip()
            if len(head) >= MIN_CLAUSE_CHARS:
                chunks, rest = [head], rest[match.end() :]
    return chunks, rest


@dataclass
class VoiceSession:
    """One open socket. Not thread-safe and doesn't need to be — every method
    runs on the one event loop this app has (plans/phase9.3.md D5)."""

    tenant: TenantConfig
    transport: Transport
    variant: str = "live"
    session_id: str = field(default_factory=lambda: f"voice:{uuid.uuid4().hex}")
    settings: Settings = field(default_factory=get_settings)
    stt: SpeechToText | None = None
    tts: TextToSpeech | None = None

    def __post_init__(self) -> None:
        self.limits = SessionLimits.from_settings(self.settings)
        self._stt = self.stt or get_stt(self.tenant)
        self._tts = self.tts or get_tts(self.tenant)
        self.turn = 0
        self.state: protocol.SessionState = "idle"
        self._opened_at = perf_counter()
        self._last_activity = self._opened_at
        self._stt_session: SttSession | None = None
        self._collector: asyncio.Task[None] | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._utterance_bytes = 0
        self._speech_started: float | None = None
        #: The mic is a session-long switch, not a per-utterance one. While
        #: it's open the STT stream stays up and the provider's own
        #: endpointing decides where one utterance ends and the next begins
        #: — so a conversation is many turns on one open mic, which is how
        #: a phone call works and how this was asked for.
        self._mic_open = False
        #: Final text accumulated since the last endpoint. Providers emit
        #: several finals for one sentence; the utterance is their
        #: concatenation, flushed when the endpoint arrives.
        self._pending_final = ""
        self._stream_done = asyncio.Event()
        #: How many turns this mic session has produced — so releasing the
        #: mic after a complete exchange isn't reported as "I didn't catch
        #: that", which would be true only of the silence since the last one.
        self._turns_this_mic = 0
        #: Set by the collector task when a cap fires there; the read loop
        #: owns the actual closing handshake, so it reads this on its poll.
        self._closing_reason: str | None = None
        #: An utterance that arrived while the bot was still thinking. It
        #: runs when that turn finishes rather than replacing it — see
        #: `_flush_pending`. Only the latest is kept.
        self._queued_text: str | None = None
        #: "instant" sends on every endpoint; "continuation" waits out a
        #: pause first, so thinking mid-sentence doesn't split the sentence.
        self.utterance_mode = self.settings.voice_utterance_mode
        #: The pending "they really have finished" timer in continuation
        #: mode. Restarted by every new endpoint, cancelled by anything that
        #: sends or closes.
        self._grace_task: asyncio.Task[None] | None = None
        #: When the provider last said the speaker stopped. §13 measures
        #: end-of-speech → first audio, and *this* is end of speech — not the
        #: moment the turn starts. Starting the clock after the continuation
        #: pause hid that pause from the number, so the reported latency was
        #: shorter than the silence the listener actually sat through, and
        #: `stt_final_ms` read a meaningless 0ms every turn.
        self._endpoint_at: float | None = None
        #: Every completed turn's metrics — what the p50/p95 line is computed
        #: from at close, and what a test asserts on.
        self.metrics: list[TurnMetrics] = []

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        await self.transport.send_json(
            protocol.ready(
                tenant_id=self.tenant.tenant_id,
                tenant_name=self.tenant.name,
                variant=self.variant,
                sample_rate_in=INPUT_SAMPLE_RATE,
                sample_rate_out=OUTPUT_SAMPLE_RATE,
                frame_ms=20,
                turn_limit=self.limits.max_turns,
                session_seconds=self.limits.max_seconds,
                stt_provider=self._stt.name,
                tts_provider=self._tts.name,
                utterance_mode=self.utterance_mode,
                continuation_pause_ms=self.settings.voice_continuation_pause_seconds * 1000,
            )
        )
        await self._set_state("idle")

    async def aclose(self) -> None:
        """Tear everything down. Must leave no orphaned task behind — the
        `aclose_calcom_mcp_sessions` lesson: a still-open async generator
        collected outside its owning task produces "attempted to exit cancel
        scope in a different task" noise and leaks the connection."""
        await self._cancel_turn()
        await self._cancel_grace()
        await self._close_stt()

    def check_caps(self) -> None:
        """Raises `SessionClosed` when a limit is reached. Called from the
        route's read loop, so one place owns the closing handshake."""
        if self._closing_reason is not None:
            # A cap hit inside the collector task, where raising would only
            # kill that task silently. It parks the reason here instead.
            raise SessionClosed(self._closing_reason)
        now = perf_counter()
        if now - self._opened_at >= self.limits.max_seconds:
            raise SessionClosed("session time limit reached")
        if now - self._last_activity >= self.limits.idle_seconds:
            raise SessionClosed("idle timeout")

    # --- client input ------------------------------------------------------

    async def handle(self, message: ClientMessage) -> None:
        self._last_activity = perf_counter()
        # `mic_on`/`mic_off` are the names that describe what these now do;
        # `start_utterance`/`end_utterance` are kept as the original wire
        # words so an older client (or a test) still works unchanged.
        if message.type in ("mic_on", "start_utterance"):
            await self._mic_on()
        elif message.type in ("mic_off", "end_utterance"):
            await self._mic_off()
        elif message.type == "mode":
            if message.text in ("instant", "continuation"):
                self.utterance_mode = message.text
                logger.info("voice utterance mode -> %s", message.text)
                if message.text == "instant":
                    # Anything already waiting out a pause should go now,
                    # rather than sitting there under the old rules.
                    await self._cancel_grace()
                    await self._flush_pending()
        elif message.type == "cancel":
            # Stops the bot talking; deliberately does NOT close the mic. The
            # mic is a switch the listener set, and "be quiet" is not "stop
            # listening to me".
            await self._cancel_turn()
            await self._set_state(self._resting_state())
        elif message.type == "text":
            text = message.text.strip()
            if text:
                await self._cancel_turn()
                self._begin_turn()
                self._speech_started = perf_counter()
                # Typed input is still "what the visitor said" — emitting it
                # as a transcript means the UI has ONE way to render a turn's
                # input instead of two that drift. Without it a typed message
                # vanished the moment it was sent.
                await self.transport.send_json(
                    protocol.transcript(text, final=True, turn=self.turn)
                )
                await self._spawn_turn(text)
        else:
            logger.debug("ignoring unknown voice message type %r", message.type)

    async def feed_audio(self, pcm: bytes) -> None:
        """A binary frame. Silently dropped unless the mic is actually open —
        a client that keeps streaming after `mic_off` (or before `mic_on`)
        must not be able to push bytes into a metered STT socket.

        Audio keeps flowing while the bot is thinking and speaking, which is
        what makes barge-in work without the listener having to press
        anything: the provider hears them start talking and endpoints it like
        any other utterance. It also means the bot can hear *itself* through
        a speaker, which is why the browser captures with
        `echoCancellation: true` (and why headphones are the honest setup).
        """
        self._last_activity = perf_counter()
        if not self._mic_open or self._stt_session is None:
            return
        self._utterance_bytes += len(pcm)
        if self._utterance_bytes > self.limits.max_utterance_bytes:
            # One utterance, not one mic session — the counter resets at every
            # endpoint, so a long conversation never trips this and a client
            # stuck transmitting still does.
            await self.transport.send_json(
                protocol.error("that went on a while — sending what I have", turn=self._next_turn)
            )
            await self._flush_pending()
            return
        try:
            await self._stt_session.send_audio(pcm)
        except Exception as exc:
            logger.warning("stt send failed tenant=%s: %s", self.tenant.tenant_id, exc)
            await self.transport.send_json(
                protocol.error("speech recognition dropped out", turn=self._next_turn)
            )
            self._mic_open = False
            await self._close_stt()
            await self._set_state("idle")

    # --- the turn ----------------------------------------------------------

    @property
    def _next_turn(self) -> int:
        """The number the utterance being spoken *right now* will become.

        Transcripts and errors that arrive while listening belong to the turn
        they are about to start, not the one that just finished — otherwise
        a UI grouping by turn files the next question under the last answer.
        """
        return self.turn + 1

    def _resting_state(self) -> protocol.SessionState:
        """Where a finished turn lands. `listening`, not `idle`, whenever the
        mic is still open — the whole point of a mic switch is that it
        survives the turn."""
        return "listening" if self._mic_open else "idle"

    async def _mic_on(self) -> None:
        if self._mic_open:
            return
        await self._close_stt()
        self._utterance_bytes = 0
        self._pending_final = ""
        self._turns_this_mic = 0
        self._stream_done = asyncio.Event()
        try:
            self._stt_session = await self._stt.open(
                sample_rate=INPUT_SAMPLE_RATE, language=self.settings.deepgram_language
            )
        except Exception as exc:
            logger.warning("stt open failed tenant=%s: %s", self.tenant.tenant_id, exc)
            await self.transport.send_json(
                protocol.error(f"speech recognition unavailable: {exc}", turn=self._next_turn)
            )
            await self._set_state("idle")
            return
        self._mic_open = True
        self._collector = asyncio.create_task(self._collect_transcripts(self._stt_session))
        await self._set_state("listening")

    async def _collect_transcripts(self, session: SttSession) -> None:
        """Read results until the stream ends, turning each endpoint into a turn.

        This is where "it should know when I stopped talking" lives, and it
        deliberately isn't a timer or a browser VAD: the STT provider already
        decides where an utterance ends (Deepgram's `speech_final` /
        `UtteranceEnd`, driven by `endpointing` + `utterance_end_ms`), and it
        decides using the audio itself rather than a guess about silence.
        """
        try:
            async for result in session.results():
                if result.text:
                    await self.transport.send_json(
                        protocol.transcript(
                            result.text, final=result.is_final, turn=self._next_turn
                        )
                    )
                if result.is_final and result.text:
                    # Providers emit several finals for a long utterance; the
                    # utterance is their concatenation, not the last one.
                    self._pending_final = f"{self._pending_final} {result.text}".strip()
                if result.speech_started and self._grace_task is not None:
                    # Sound, not yet words. Deferring the send on this is
                    # free if it turns out to be nothing — the countdown
                    # simply restarts at the next endpoint — and it is the
                    # only signal early enough to beat the countdown.
                    await self._cancel_grace()
                if result.text and self._grace_task is not None:
                    # They are still talking, so the pause that looked like
                    # the end of a sentence wasn't one. Cancel the countdown
                    # on the first *word* — waiting for the next endpoint to
                    # restart it is too late, because the countdown is
                    # shorter than the sentence that interrupts it. Live
                    # proof: "I want to" [0.8s] "book a room" still split in
                    # two until this line existed.
                    await self._cancel_grace()
                if result.text and self.state == "speaking":
                    # They started talking over the answer. Stop *now*, on the
                    # first word, rather than at the end of their sentence —
                    # waiting means the bot talks through their interruption,
                    # which is the thing that makes a voice bot feel deaf.
                    await self._cancel_turn()
                    await self._set_state(self._resting_state())
                if result.speech_final:
                    await self._endpoint_reached()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("stt stream failed tenant=%s: %s", self.tenant.tenant_id, exc)
        finally:
            self._stream_done.set()

    async def _endpoint_reached(self) -> None:
        """The provider says the speaker stopped. Whether that ends the turn
        depends on the mode.

        `instant` sends immediately. `continuation` treats it as *probably*
        finished and waits `voice_continuation_pause_seconds`: if more speech
        arrives, the next endpoint restarts the wait and the text keeps
        accumulating, so "I want to…" + "…book a room" stays one question
        instead of becoming two — one of which the bot would answer on its
        own, confidently and wrongly.
        """
        self._endpoint_at = perf_counter()
        if self.utterance_mode == "instant":
            await self._flush_pending()
            return
        await self._cancel_grace()
        self._grace_task = asyncio.create_task(self._wait_then_flush())

    async def _wait_then_flush(self) -> None:
        try:
            await asyncio.sleep(self.settings.voice_continuation_pause_seconds)
        except asyncio.CancelledError:
            raise
        await self._flush_pending()

    async def _cancel_grace(self) -> None:
        task, self._grace_task = self._grace_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _flush_pending(self) -> None:
        """An endpoint arrived: turn whatever has accumulated into a turn.

        Silence between utterances reaches here with nothing pending, which
        is the normal case on an open mic and must stay free — a bot that
        answered every pause would never stop talking.
        """
        text, self._pending_final = self._pending_final.strip(), ""
        self._utterance_bytes = 0
        if not text:
            return
        # The §13 clock starts at end-of-speech — the endpoint itself, so a
        # continuation pause counts against the budget rather than hiding
        # inside it. A typed message has no endpoint and starts now.
        self._speech_started = self._endpoint_at or perf_counter()
        self._endpoint_at = None

        busy = self._turn_task is not None and not self._turn_task.done()
        if busy and self.state == "speaking":
            # Words you can hear, you can interrupt.
            await self._cancel_turn()
            busy = False
        if busy:
            # The bot is *thinking* — it has said nothing yet, so there is
            # nothing to interrupt, and cancelling would throw away an answer
            # that is seconds from arriving. That exact behaviour, when the
            # trigger was a stray tap, is what made a live session read as
            # the bot ignoring the question until it was asked three times.
            # An open mic makes the trigger far more likely (a cough, an
            # "umm"), so the answer is to queue rather than to discard: both
            # questions get answered, in the order they were asked.
            self._queued_text = text
            return
        await self._start_turn(text)

    async def _start_turn(self, text: str) -> None:
        try:
            self._begin_turn()
        except SessionClosed as closed:
            # Raising out of the collector task would kill it silently; the
            # read loop owns the closing handshake.
            self._closing_reason = closed.reason
            return
        self._turns_this_mic += 1
        await self._spawn_turn(text)

    async def _mic_off(self) -> None:
        if not self._mic_open or self._stt_session is None:
            return
        self._mic_open = False
        # Turning the mic off is a definite end: whatever is mid-pause goes
        # now rather than waiting out a silence that will never be broken.
        await self._cancel_grace()
        try:
            await self._stt_session.finish()
        except Exception as exc:
            logger.warning("stt finish failed tenant=%s: %s", self.tenant.tenant_id, exc)

        # Wait for the provider's last results rather than cutting the stream
        # off: a cut-off final is worse than a slow one, and the speaker has
        # already stopped talking by the time we get here.
        try:
            await asyncio.wait_for(self._stream_done.wait(), timeout=self.limits.stt_final_timeout)
        except TimeoutError:
            logger.warning("stt final timed out tenant=%s", self.tenant.tenant_id)

        had_pending = bool(self._pending_final.strip())
        await self._flush_pending()
        await self._close_stt()

        if not had_pending and self._turns_this_mic == 0:
            await self.transport.send_json(
                protocol.error("I didn't catch that — try again.", turn=self._next_turn)
            )
        if self._turn_task is None or self._turn_task.done():
            await self._set_state("idle")

    def _begin_turn(self) -> None:
        self.turn += 1
        if self.turn > self.limits.max_turns:
            raise SessionClosed("turn limit reached")

    async def _spawn_turn(self, text: str) -> None:
        """The brain runs in its own task so the read loop keeps serving —
        which is what lets `cancel` (or a client disconnect) interrupt a turn
        already in flight rather than waiting it out."""
        self._turn_task = asyncio.create_task(self._run_turn(text))

    async def join_turn(self) -> None:
        """Await the in-flight turn, if there is one.

        Used by tests, and by nothing on the request path: the route must
        never block on a turn, because a read loop waiting for the brain is
        a read loop that can't hear `cancel`.
        """
        task = self._turn_task
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _cancel_turn(self) -> None:
        task, self._turn_task = self._turn_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run_turn(self, text: str) -> None:
        started = self._speech_started or perf_counter()
        record = TurnMetrics(turn=self.turn)
        buffer = ""
        spoke_anything = False
        #: Did anything beyond the "bear with me" filler get said? A turn that
        #: ends having spoken only its acknowledgement is the shape of a
        #: failure a listener experiences as the bot going quiet mid-answer —
        #: reported live, and invisible at the time because the error line was
        #: being overwritten by the metrics line. It leaves a log entry now.
        answered = False
        try:
            await self._set_state("thinking")
            record.stt_final_ms = (perf_counter() - started) * 1000

            async for event in stream_turn(
                text=text,
                tenant_id=self.tenant.tenant_id,
                session_id=self.session_id,
                channel="voice",
                tenant_config_variant="draft" if self.variant == "draft" else "live",
            ):
                if event.type == "error":
                    await self.transport.send_json(
                        protocol.error(event.text or "the turn failed", turn=self.turn)
                    )
                    continue
                if not event.is_spoken:
                    # Tool events are logged and nothing else. Letting one
                    # reach audio is how a caller ends up hearing raw tool
                    # output read aloud.
                    logger.debug("voice turn %s: %s %s", self.turn, event.type, event.tool)
                    continue
                if event.type == "token":
                    answered = True
                if record.first_token_ms is None:
                    record.first_token_ms = (perf_counter() - started) * 1000
                await self.transport.send_json(protocol.token(event.text, self.turn))
                buffer += event.text
                chunks, buffer = _take_chunks(buffer, allow_clause_flush=not spoke_anything)
                for chunk in chunks:
                    spoke_anything = True
                    await self._speak(chunk, record, started)

            tail = buffer.strip()
            if tail:
                await self._speak(tail, record, started)
        except asyncio.CancelledError:
            logger.info("voice turn %s cancelled tenant=%s", self.turn, self.tenant.tenant_id)
            record.interrupted = True
            raise
        except Exception as exc:
            logger.exception("voice turn failed tenant=%s", self.tenant.tenant_id)
            await self.transport.send_json(protocol.error(str(exc), turn=self.turn))
        finally:
            record.total_ms = (perf_counter() - started) * 1000
            self.metrics.append(record)
            with contextlib.suppress(Exception):
                await self.transport.send_json(record.as_message())
                # Back to listening, not idle, when the mic is still open —
                # the next question needs no click.
                await self._set_state(self._resting_state())
            if not answered and not isinstance(sys.exc_info()[1], asyncio.CancelledError):
                logger.warning(
                    "voice turn %d spoke only an acknowledgement tenant=%s session=%s "
                    "asked=%r — the caller heard the filler and then silence",
                    record.turn,
                    self.tenant.tenant_id,
                    self.session_id,
                    text[:120],
                )
            self._log_turn(record)
            # Anything said while this turn was thinking runs now, in the
            # order it was asked. Its clock started when the speaker stopped,
            # so the latency it reports includes this wait — which is what
            # the listener actually experienced.
            queued, self._queued_text = self._queued_text, None
            if queued:
                with contextlib.suppress(Exception):
                    await self._start_turn(queued)

    async def _speak(self, chunk: str, record: TurnMetrics, started: float) -> None:
        """Synthesize one chunk and push it out, frame by frame.

        Sequential per chunk, not pipelined: chunk N+1's audio must not
        overtake chunk N's on a transport that preserves order but not
        interleaving. The latency that matters is the *first* chunk's, and
        that is already as early as it can be.
        """
        if self.state != "speaking":
            await self._set_state("speaking")
        # A synthesizer chunks on its own schedule, and Cartesia really does
        # return odd-length chunks (6 of 32 in a measured reply). PCM16 is two
        # bytes a sample, so an odd chunk splits a sample across the boundary:
        # the trailing byte would be sent as a frame the browser can't decode
        # (`new Int16Array` requires an even byte length), and the listener
        # hears a click. Carry the stray byte into the next chunk instead —
        # this was the reported "the voice is noisy".
        carry = b""
        try:
            async for pcm in self._tts.synthesize(
                chunk, voice=self.tenant.voice, sample_rate=OUTPUT_SAMPLE_RATE
            ):
                if not pcm:
                    continue
                pcm = carry + pcm
                if len(pcm) % 2:
                    pcm, carry = pcm[:-1], pcm[-1:]
                else:
                    carry = b""
                if not pcm:
                    continue
                if record.first_audio_ms is None:
                    record.first_audio_ms = (perf_counter() - started) * 1000
                for frame in iter_frames(pcm, OUTPUT_SAMPLE_RATE):
                    await self.transport.send_bytes(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # One unspeakable sentence costs that sentence's audio, never the
            # rest of the reply — the text already reached the client.
            logger.warning("tts failed tenant=%s: %s", self.tenant.tenant_id, exc)
            await self.transport.send_json(
                protocol.error(f"speech synthesis failed: {exc}", turn=self.turn)
            )

    # --- plumbing ----------------------------------------------------------

    async def _set_state(self, value: protocol.SessionState) -> None:
        self.state = value
        # Always before a turn's first audio frame, never after: binary frames
        # carry no turn of their own and inherit this one (D6).
        await self.transport.send_json(protocol.state(value, self.turn))

    async def _close_stt(self) -> None:
        collector, self._collector = self._collector, None
        session, self._stt_session = self._stt_session, None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.aclose()
        if collector is not None and not collector.done():
            collector.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await collector

    def _log_turn(self, record: TurnMetrics) -> None:
        logger.info(
            "voice turn complete tenant=%s session=%s turn=%d "
            "stt_final_ms=%s first_token_ms=%s first_audio_ms=%s total_ms=%s",
            self.tenant.tenant_id,
            self.session_id,
            record.turn,
            _round(record.stt_final_ms),
            _round(record.first_token_ms),
            _round(record.first_audio_ms),
            _round(record.total_ms),
        )


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile, no numpy — the same "pure Python, no new
    dependency" call `app/db/memory_store.py::_cosine_similarity` made.

    `ceil`, not `round`: Python's `round` is banker's rounding, which put the
    p50 of two samples on the *higher* one. A latency percentile that reports
    the worse of two numbers as the median is the wrong direction to be wrong
    in for the one measurement this phase exists to produce.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def summarise(metrics: list[TurnMetrics]) -> dict[str, float | int | None]:
    """p50/p95 first-audio across a session — the §13 answer, logged at close."""
    first_audio = [m.first_audio_ms for m in metrics if m.first_audio_ms is not None]
    return {
        "turns": len(metrics),
        "first_audio_p50_ms": _round(percentile(first_audio, 0.50)),
        "first_audio_p95_ms": _round(percentile(first_audio, 0.95)),
    }


__all__ = [
    "SessionClosed",
    "SessionLimits",
    "Transport",
    "TurnMetrics",
    "VoiceSession",
    "summarise",
]
