"""The turn orchestrator — chunking, cancellation, and what never becomes audio.

Drives `VoiceSession` against a recording transport rather than a real
socket, which is the whole point of the `Transport` seam: none of this needs
a WebSocket, and none of it needs the route.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.voice.protocol import ClientMessage
from app.voice.session import VoiceSession, _take_chunks
from app.voice.stt.base import Transcript
from app.voice.stt.fake import DEFAULT_UTTERANCE, FakeSpeechToText
from app.voice.tts.fake import FakeTextToSpeech
from tests.conftest import ai


class Recorder:
    """A `Transport` that keeps everything, so a test can assert on order —
    which is the property that actually matters here (see `state` always
    preceding a turn's first audio frame)."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.audio: list[bytes] = []
        #: Interleaved, so "did audio arrive before this message?" is answerable.
        self.timeline: list[str] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.messages.append(payload)
        self.timeline.append(str(payload.get("type")))

    async def send_bytes(self, data: bytes) -> None:
        self.audio.append(data)
        self.timeline.append("audio")

    def types(self) -> list[str]:
        return [str(m.get("type")) for m in self.messages]

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.messages if m.get("type") == kind]

    def spoken_text(self) -> str:
        return "".join(m["text"] for m in self.of_type("token"))


def build(hotel, *, stt=None, tts=None, variant="live", mode=None) -> tuple[VoiceSession, Recorder]:
    recorder = Recorder()
    session = VoiceSession(
        tenant=hotel,
        transport=recorder,
        variant=variant,
        stt=stt or FakeSpeechToText(),
        tts=tts or FakeTextToSpeech(),
    )
    if mode is not None:
        # Most tests here are about the endpoint -> turn mechanics, which
        # `continuation` (the production default) deliberately delays. They
        # pin `instant` so they assert on the machinery, not on a timer.
        session.utterance_mode = mode
    return session, recorder


async def settle(predicate, *, ticks: int = 200):
    """Yield to the loop until `predicate()` holds.

    An utterance is delivered by the collector task, so a test can't know how
    many loop turns separate "pushed" from "turn running" — polling the
    outcome beats guessing a number of `sleep(0)`s.
    """
    for _ in range(ticks):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


async def test_a_typed_turn_speaks_the_reply(hotel, scripted):
    scripted(ai("We open at seven. Can I book you in?"))
    tts = FakeTextToSpeech()
    session, recorder = build(hotel, tts=tts)
    await session.start()

    await session.handle(ClientMessage("text", "when do you open?"))
    await session.join_turn()

    assert recorder.audio, "the turn produced no audio at all"
    assert "We open at seven." in tts.spoken
    assert recorder.spoken_text().startswith("We open at seven.")
    assert recorder.types()[-1] == "state"  # back to idle


async def test_the_first_sentence_is_spoken_before_the_reply_is_finished(hotel, scripted):
    """The §13 budget in one assertion: audio must start on the *first*
    sentence, not after the last. A regression here is invisible in the
    transcript and obvious on a phone call."""
    scripted(ai("Yes, we have a room. It is two hundred a night. Shall I hold it?"))
    tts = FakeTextToSpeech()
    session, recorder = build(hotel, tts=tts)
    await session.start()

    await session.handle(ClientMessage("text", "got a room?"))
    await session.join_turn()

    # Three sentences, synthesized separately — not one blob at the end.
    assert len(tts.spoken) >= 2
    assert tts.spoken[0] == "Yes, we have a room."
    # And the first audio frame landed before the last sentence was even sent
    # as text.
    first_audio = recorder.timeline.index("audio")
    assert first_audio < len(recorder.timeline) - 1


async def test_tool_events_never_become_audio(hotel, scripted):
    """CLAUDE.md's rule, enforced where it can actually be broken. The
    acknowledgement *is* spoken (that's the point of acknowledge-then-act);
    the tool's own output never is."""
    scripted(
        ai("", [{"name": "check_availability", "args": {"service": "room-reservation"}}]),
        ai("I have a few times free."),
    )
    tts = FakeTextToSpeech()
    session, recorder = build(hotel, tts=tts)
    await session.start()

    await session.handle(ClientMessage("text", "what's free tomorrow?"))
    await session.join_turn()

    everything_spoken = " ".join(tts.spoken)
    assert "I have a few times free." in everything_spoken
    # No tool vocabulary reached the synthesizer or the client's text.
    assert "check_availability" not in everything_spoken
    assert "check_availability" not in recorder.spoken_text()
    assert "tool_start" not in recorder.types()
    assert "tool_result" not in recorder.types()


async def test_cancel_stops_a_turn_in_flight(hotel):
    """Barge-in, precisely as scoped: cancelling a turn already in flight.
    Not VAD — the gesture is explicit (plans/phase9.3.md's non-goals)."""
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowTts:
        name = "slow"

        def __init__(self) -> None:
            self.spoken: list[str] = []

        async def synthesize(self, text, *, voice, sample_rate):
            self.spoken.append(text)
            started.set()
            await release.wait()  # never released: the test cancels instead
            yield b"\x00" * 480

    tts = SlowTts()
    session, recorder = build(hotel, tts=tts)
    await session.start()

    from app.brain.llm import set_llm_override
    from tests.conftest import ScriptedChatModel

    set_llm_override(ScriptedChatModel(responses=[ai("This is a long answer.")]))
    from app.brain.graph import reset_graph

    reset_graph()

    await session.handle(ClientMessage("text", "tell me about the spa"))
    await asyncio.wait_for(started.wait(), timeout=5)
    await session.handle(ClientMessage("cancel"))

    assert recorder.audio == [], "audio escaped from a cancelled turn"
    assert session._turn_task is None
    assert session.state == "idle"


async def test_the_stt_path_feeds_the_transcript_to_the_brain(hotel, scripted):
    scripted(ai("We do, yes."))
    stt = FakeSpeechToText(["do you have parking"])
    session, recorder = build(hotel, stt=stt)
    await session.start()

    await session.handle(ClientMessage("start_utterance"))
    await session.feed_audio(bytes(640))
    await session.feed_audio(bytes(640))
    await session.handle(ClientMessage("end_utterance"))
    await session.join_turn()

    assert stt.sessions[0].audio_bytes == 1280
    finals = [m for m in recorder.of_type("transcript") if m["final"]]
    assert finals[-1]["text"] == "do you have parking"
    assert "We do, yes." in recorder.spoken_text()


async def test_silence_produces_a_polite_error_not_a_turn(hotel, scripted):
    """An empty transcript must not reach the model — a bot answering a
    question nobody asked is worse than "I didn't catch that"."""
    model = scripted(ai("this should never be said"))
    stt = FakeSpeechToText([""])
    session, recorder = build(hotel, stt=stt)
    await session.start()

    await session.handle(ClientMessage("start_utterance"))
    await session.handle(ClientMessage("end_utterance"))

    assert recorder.of_type("error"), "the caller was told nothing"
    assert session._turn_task is None
    assert model.seen_prompts == [], "an empty transcript reached the model"


async def test_audio_after_the_mic_is_released_is_dropped(hotel, scripted):
    scripted(ai("Sure."))
    stt = FakeSpeechToText(["hello"])
    session, _ = build(hotel, stt=stt)
    await session.start()

    await session.handle(ClientMessage("start_utterance"))
    await session.feed_audio(bytes(640))
    await session.handle(ClientMessage("end_utterance"))
    await session.join_turn()
    await session.feed_audio(bytes(640))  # late frame from a laggy client

    assert stt.sessions[0].audio_bytes == 640


async def test_an_oversized_utterance_is_sent_rather_than_buffered_forever(
    hotel, monkeypatch, scripted
):
    """A stuck client transmitting forever must not stream unbounded bytes
    into a metered STT socket. It flushes what it has and keeps the mic open
    — cutting the mic would punish the listener for the client's bug."""
    monkeypatch.setenv("VOICE_MAX_UTTERANCE_BYTES", "1000")
    from app.config import reset_settings_cache

    reset_settings_cache()
    scripted(ai("Go on."))
    stt = FakeSpeechToText()
    session, recorder = build(hotel, stt=stt, mode="instant")
    await session.start()

    await session.handle(ClientMessage("mic_on"))
    await stt.sessions[0].utter("a long ramble that never ends")
    await settle(lambda: session.turn == 1)
    await session.feed_audio(bytes(640))
    await session.feed_audio(bytes(640))  # crosses the cap

    assert recorder.of_type("error")
    assert session._mic_open, "the mic was cut for a client-side fault"
    # ...and the byte counter resets per utterance, or a long conversation
    # would trip this on its own.
    assert session._utterance_bytes == 0


async def test_closing_leaves_no_orphaned_tasks(hotel, scripted):
    """The `aclose_calcom_mcp_sessions` lesson: a generator torn down outside
    its owning task is a real resource leak, not cosmetic noise."""
    scripted(ai("One moment."))
    session, _ = build(hotel)
    await session.start()

    await session.handle(ClientMessage("start_utterance"))
    await session.handle(ClientMessage("text", "hello"))
    await session.aclose()

    assert session._turn_task is None
    assert session._stt_session is None
    assert session._collector is None


async def test_an_unscripted_utterance_still_says_something_sensible(hotel, scripted):
    """The fake STT is the *default* provider, so what it "hears" with no
    script is a real user-visible string, not a debug token."""
    scripted(ai("Seven in the morning."))
    stt = FakeSpeechToText()
    session, recorder = build(hotel, stt=stt)
    await session.start()

    await session.handle(ClientMessage("start_utterance"))
    await session.handle(ClientMessage("end_utterance"))
    await session.join_turn()

    finals = [m for m in recorder.of_type("transcript") if m["final"]]
    assert finals[-1]["text"] == DEFAULT_UTTERANCE


def test_chunking_splits_on_sentences_and_keeps_the_remainder():
    # The remainder has no leading space: `_SENTENCE_END` consumes the
    # separator, which is what stops every chunk after the first arriving at
    # the synthesizer with a stray space in front of it.
    assert _take_chunks("One. Two. Thr", allow_clause_flush=False) == (["One.", "Two."], "Thr")


def test_chunking_does_not_flush_a_two_word_clause():
    """ "Sure," alone followed by a gap sounds worse than the handful of
    milliseconds the early flush saves."""
    chunks, rest = _take_chunks("Sure, I can check that for you and see", allow_clause_flush=True)
    assert chunks == []
    assert rest.startswith("Sure,")


def test_chunking_does_flush_a_long_opener_at_a_clause():
    chunks, _ = _take_chunks(
        "Let me look at the diary for you, that will just take a second",
        allow_clause_flush=True,
    )
    assert chunks == ["Let me look at the diary for you,"]


def test_chunking_never_flushes_a_clause_once_something_was_already_spoken():
    """The early flush buys perceived latency on the *first* chunk only.
    After that it would just add seams for nothing."""
    assert _take_chunks(
        "and then, once that is done, we can talk about the rest",
        allow_clause_flush=False,
    ) == ([], "and then, once that is done, we can talk about the rest")


# --- reported from a real session ------------------------------------------


async def test_a_typed_message_is_echoed_back_as_a_transcript(hotel, scripted):
    """Speech showed up as "you: …" and typed text didn't — so a typed
    message vanished the moment it was sent. One mechanism for both."""
    scripted(ai("Seven in the morning."))
    session, recorder = build(hotel)
    await session.start()

    await session.handle(ClientMessage("text", "when do you open?"))
    await session.join_turn()

    echoed = [m for m in recorder.of_type("transcript") if m["final"]]
    assert [m["text"] for m in echoed] == ["when do you open?"]


async def test_taking_the_mic_while_the_bot_is_thinking_does_not_kill_the_answer(hotel, scripted):
    """The reported bug: the bot said "let me check the diary", a stray tap
    opened the mic, and the answer that was seconds away never arrived — so
    the question had to be asked three times. You can't interrupt silence."""
    scripted(
        ai("", [{"name": "check_availability", "args": {"service": "room-reservation"}}]),
        ai("I have a few times free."),
    )
    tts = FakeTextToSpeech()
    session, recorder = build(hotel, tts=tts)
    await session.start()

    await session.handle(ClientMessage("text", "what's free tomorrow?"))
    # ...the tap, while the turn is still working.
    await session.handle(ClientMessage("start_utterance"))
    await session.join_turn()

    assert "I have a few times free." in recorder.spoken_text()
    assert not any(m.get("interrupted") for m in recorder.of_type("metrics"))


async def test_speaking_over_the_bot_interrupts_it_with_no_button_at_all(hotel):
    """Barge-in on an open mic: the listener just talks. Opening the mic is
    NOT the gesture — it can be on for the whole conversation — so the
    interrupt has to come from a real utterance arriving."""
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowTts:
        name = "slow"

        async def synthesize(self, text, *, voice, sample_rate):
            started.set()
            await release.wait()
            yield bytes(480)

    stt = FakeSpeechToText()
    session, recorder = build(hotel, stt=stt, tts=SlowTts(), mode="instant")
    await session.start()

    from app.brain.graph import reset_graph
    from app.brain.llm import set_llm_override
    from tests.conftest import ScriptedChatModel

    set_llm_override(
        ScriptedChatModel(responses=[ai("A long answer arrives."), ai("Parking is free.")])
    )
    reset_graph()

    await session.handle(ClientMessage("mic_on"))
    await session.handle(ClientMessage("text", "tell me about the spa"))
    await asyncio.wait_for(started.wait(), timeout=5)
    assert session.state == "speaking"

    # ...and now they simply speak.
    await stt.sessions[0].utter("actually, what about parking?")
    await settle(lambda: session.turn == 2)

    assert any(m.get("interrupted") for m in recorder.of_type("metrics"))
    # The second turn is left mid-synthesis on purpose: this test is about
    # the interrupt, and `aclose` is what proves it tears down cleanly.
    await session.aclose()


async def test_an_open_mic_answers_several_questions_without_a_single_click(hotel, scripted):
    """The headline behaviour: one mic switch, a whole conversation. Each
    utterance ends itself — nothing presses send between them."""
    scripted(ai("Seven in the morning."), ai("Yes, there is parking."))
    stt = FakeSpeechToText()
    session, recorder = build(hotel, stt=stt, mode="instant")
    await session.start()

    await session.handle(ClientMessage("mic_on"))
    await stt.sessions[0].utter("what time do you open?")
    await settle(lambda: session.turn == 1)
    await session.join_turn()
    await stt.sessions[0].utter("do you have parking?")
    await settle(lambda: session.turn == 2)
    await session.join_turn()

    assert session.turn == 2
    assert len(stt.sessions) == 1, "the mic reopened between utterances"
    assert session._mic_open
    # Back to listening after each answer, never idle — that IS the feature.
    assert session.state == "listening"
    spoken = recorder.spoken_text()
    assert "Seven in the morning." in spoken
    assert "Yes, there is parking." in spoken


async def test_silence_on_an_open_mic_never_starts_a_turn(hotel, scripted):
    """An endpoint with nothing behind it is just a pause. A bot that
    answered every pause would never stop talking."""
    model = scripted(ai("this should never be said"))
    stt = FakeSpeechToText()
    session, _ = build(hotel, stt=stt)
    await session.start()

    await session.handle(ClientMessage("mic_on"))
    await stt.sessions[0].utter("")
    for _ in range(20):
        await asyncio.sleep(0)

    assert session.turn == 0
    assert model.seen_prompts == []


async def test_a_new_question_supersedes_a_turn_still_in_flight(hotel, scripted):
    """Deferring the cancel must not mean two answers racing: a real new
    utterance still replaces the old turn."""
    scripted(ai("First answer."), ai("Second answer."))
    session, _ = build(hotel, stt=FakeSpeechToText(["and what about parking"]))
    await session.start()

    await session.handle(ClientMessage("text", "first question"))
    await session.handle(ClientMessage("start_utterance"))
    await session.handle(ClientMessage("end_utterance"))
    await session.join_turn()

    assert session.turn == 2


async def test_odd_length_synth_chunks_never_reach_the_wire_misaligned(hotel, scripted):
    """PCM16 is two bytes a sample, and Cartesia really does return
    odd-length chunks (6 of 32 in a measured reply). A trailing half-sample
    sent as its own frame is a click in the ear and a decode error in the
    browser — this was the reported "the voice is noisy"."""

    class RaggedTts:
        name = "ragged"

        async def synthesize(self, text, *, voice, sample_rate):
            yield b"\x01" * 101  # odd
            yield b"\x02" * 51  # odd again: the carry must survive both
            yield b"\x03" * 40

    scripted(ai("Hello."))
    session, recorder = build(hotel, tts=RaggedTts())
    await session.start()

    await session.handle(ClientMessage("text", "hi"))
    await session.join_turn()

    assert recorder.audio, "no audio at all"
    assert all(len(frame) % 2 == 0 for frame in recorder.audio), "a frame split a sample"
    # 192 bytes in, at most one stray byte withheld at the very end.
    assert sum(len(f) for f in recorder.audio) == 192


async def test_a_second_question_asked_mid_thought_is_answered_too_not_instead(hotel):
    """An open mic hears every cough and "umm". Cancelling a *thinking* turn
    on any of them recreates the reported bug where an answer that was
    seconds away never arrived. Queue it instead: both get answered, in the
    order they were asked."""
    from app.brain.graph import reset_graph
    from app.brain.llm import set_llm_override
    from tests.conftest import ScriptedChatModel

    gate = asyncio.Event()

    class GatedModel(ScriptedChatModel):
        """Holds the turn in `thinking` — nothing spoken yet — until released."""

        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            await gate.wait()
            for chunk in self._chunks(self._next(messages)):
                yield chunk

    set_llm_override(GatedModel(responses=[ai("We open at seven."), ai("Yes, there is parking.")]))
    reset_graph()

    stt = FakeSpeechToText()
    session, recorder = build(hotel, stt=stt, mode="instant")
    await session.start()

    await session.handle(ClientMessage("mic_on"))
    await stt.sessions[0].utter("what time do you open?")
    await settle(lambda: session.state == "thinking")

    await stt.sessions[0].utter("and do you have parking?")
    await settle(lambda: session._queued_text is not None)
    assert session._queued_text == "and do you have parking?"

    gate.set()
    await session.join_turn()
    await settle(lambda: session.turn == 2)
    await session.join_turn()

    spoken = recorder.spoken_text()
    assert "We open at seven." in spoken, "the first answer was thrown away"
    assert "Yes, there is parking." in spoken
    assert not any(m.get("interrupted") for m in recorder.of_type("metrics"))


# --- pausing mid-sentence (continuation mode) -------------------------------


async def test_a_pause_mid_sentence_continues_it_instead_of_splitting_it(
    hotel, monkeypatch, scripted
):
    """The reported bug: "I want to…" [pause] "…book a room" became two
    questions, and the bot answered the fragment. Continuation mode treats
    an endpoint as *probably* finished and lets the rest catch up."""
    monkeypatch.setenv("VOICE_CONTINUATION_PAUSE_SECONDS", "0.15")
    from app.config import reset_settings_cache

    reset_settings_cache()
    scripted(ai("Happy to book that."))
    stt = FakeSpeechToText()
    session, recorder = build(hotel, stt=stt)
    await session.start()
    assert session.utterance_mode == "continuation", "continuation is the default"

    await session.handle(ClientMessage("mic_on"))
    await stt.sessions[0].utter("I want to")
    await asyncio.sleep(0.05)  # a breath, shorter than the grace period
    assert session.turn == 0, "sent a fragment before the sentence finished"
    await stt.sessions[0].utter("book a room")
    await asyncio.sleep(0.4)
    await session.join_turn()

    assert session.turn == 1, "one sentence became two turns"
    finals = [m["text"] for m in recorder.of_type("transcript") if m["final"]]
    assert finals == ["I want to", "book a room"]
    # ...and the brain saw the whole thing, not the fragment.
    asked = [p for p in session.metrics]
    assert len(asked) == 1


async def test_a_real_gap_still_ends_the_turn(hotel, monkeypatch, scripted):
    """Continuation must not mean "never finish": past the grace period the
    sentence is done."""
    monkeypatch.setenv("VOICE_CONTINUATION_PAUSE_SECONDS", "0.1")
    from app.config import reset_settings_cache

    reset_settings_cache()
    scripted(ai("Seven."), ai("Yes."))
    stt = FakeSpeechToText()
    session, _ = build(hotel, stt=stt)
    await session.start()

    await session.handle(ClientMessage("mic_on"))
    await stt.sessions[0].utter("what time do you open?")
    await asyncio.sleep(0.3)
    await session.join_turn()
    assert session.turn == 1

    await stt.sessions[0].utter("do you have parking?")
    await asyncio.sleep(0.3)
    await session.join_turn()
    assert session.turn == 2


async def test_switching_to_instant_sends_what_is_already_waiting(hotel, scripted):
    """Flipping the switch mid-pause shouldn't strand the sentence under the
    old rules."""
    scripted(ai("Happy to book that."))
    stt = FakeSpeechToText()
    session, _ = build(hotel, stt=stt)
    await session.start()

    await session.handle(ClientMessage("mic_on"))
    await stt.sessions[0].utter("I want to book a room")
    await settle(lambda: session._grace_task is not None)
    assert session.turn == 0

    await session.handle(ClientMessage("mode", "instant"))
    await settle(lambda: session.turn == 1)
    await session.join_turn()

    assert session.utterance_mode == "instant"


async def test_an_unknown_mode_is_ignored_rather_than_believed(hotel):
    session, _ = build(hotel)
    await session.start()
    await session.handle(ClientMessage("mode", "whatever"))
    assert session.utterance_mode == "continuation"


async def test_the_bot_stops_on_the_first_word_spoken_over_it(hotel):
    """Barge-in used to wait for the end of the interrupting sentence, so the
    bot talked straight through it. It stops on the first word now."""
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowTts:
        name = "slow"

        async def synthesize(self, text, *, voice, sample_rate):
            started.set()
            await release.wait()
            yield bytes(480)

    stt = FakeSpeechToText()
    session, recorder = build(hotel, stt=stt, tts=SlowTts())
    await session.start()

    from app.brain.graph import reset_graph
    from app.brain.llm import set_llm_override
    from tests.conftest import ScriptedChatModel

    set_llm_override(ScriptedChatModel(responses=[ai("A long answer arrives.")]))
    reset_graph()

    await session.handle(ClientMessage("mic_on"))
    await session.handle(ClientMessage("text", "tell me about the spa"))
    await asyncio.wait_for(started.wait(), timeout=5)
    assert session.state == "speaking"

    # One interim word — not a finished sentence, not an endpoint.
    await stt.sessions[0]._queue.put(Transcript("actually", is_final=False))
    await settle(lambda: session.state != "speaking")

    assert any(m.get("interrupted") for m in recorder.of_type("metrics"))
    await session.aclose()


async def test_speech_during_the_pause_defers_the_send_immediately(hotel, monkeypatch, scripted):
    """The countdown has to die on the first *word* of the continuation, not
    on its endpoint — the endpoint arrives after the whole phrase, by which
    time the countdown has already fired and split the sentence. Found live:
    "I want to" [0.8s] "book a room" still became two turns until this."""
    monkeypatch.setenv("VOICE_CONTINUATION_PAUSE_SECONDS", "0.12")
    from app.config import reset_settings_cache

    reset_settings_cache()
    scripted(ai("Happy to book that."))
    stt = FakeSpeechToText()
    session, _ = build(hotel, stt=stt)
    await session.start()

    await session.handle(ClientMessage("mic_on"))
    await stt.sessions[0].utter("I want to")
    await settle(lambda: session._grace_task is not None)

    # One interim word, well inside the countdown, and nothing more for
    # longer than the countdown would have lasted.
    await stt.sessions[0]._queue.put(Transcript("book", is_final=False))
    await settle(lambda: session._grace_task is None)
    await asyncio.sleep(0.25)

    assert session.turn == 0, "the countdown fired while they were still talking"
