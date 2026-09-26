"""The `/voice/live` wire vocabulary — one source of truth for both ends.

plans/phase9.3.md D6 is the spec; this module is its only implementation, so
the browser client and the server can't drift into two dialects the way two
hand-rolled JSON shapes always do.

**Binary frames are audio, text frames are control.** WebSocket frames are
already typed, so the split costs nothing and avoids base64'ing a 640-byte
PCM frame into JSON ~50 times a second per direction for no benefit.

**Every server message carries `turn`.** That is load-bearing, not
decorative: after a `cancel`, TTS frames already handed to the transport can
still arrive, and a client with no way to tell which turn a frame belongs to
will happily play the audio of a turn the user just abandoned. Binary frames
inherit the turn from the most recent `state` message, which is why `state`
is always sent *before* a turn's first audio frame and never after.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

#: Server-side view of the session. `idle` means "nobody holds the floor".
SessionState = Literal["idle", "listening", "thinking", "speaking"]


@dataclass(frozen=True, slots=True)
class ClientMessage:
    """A parsed client control frame.

    `type` is deliberately a plain `str`, not a `Literal`: this is
    attacker-reachable input (anyone with a test link), so unknown types must
    be a value the session can ignore, never a parse error that kills the
    socket.
    """

    type: str
    text: str = ""

    #: The route drops anything not named here *before* the session sees it,
    #: so a type added to `VoiceSession.handle` and not to this tuple is
    #: silently inert over a real socket while every offline test — which
    #: calls `handle()` directly — still passes. That is exactly how
    #: `mic_on`/`mic_off` shipped broken for one live run.
    KNOWN = (
        "mic_on",
        "mic_off",
        "start_utterance",
        "end_utterance",
        "cancel",
        "text",
        "mode",
    )

    @property
    def is_known(self) -> bool:
        return self.type in self.KNOWN


def parse_client_message(payload: Any) -> ClientMessage | None:
    """Never raises. Malformed input is `None` and gets ignored upstream —
    the same fail-closed posture `verify_test_token` takes, for the same
    reason: one bad frame from a flaky browser must not end a live session.
    """
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    if not isinstance(kind, str) or not kind:
        return None
    # `value` rides in `text`: one optional string field covers every client
    # message that carries anything, and a second would only be another
    # thing to forget to parse.
    payload_text = payload.get("text")
    if not isinstance(payload_text, str):
        payload_text = payload.get("value")
    return ClientMessage(type=kind, text=payload_text if isinstance(payload_text, str) else "")


def ready(
    *,
    tenant_id: str,
    tenant_name: str,
    variant: str,
    sample_rate_in: int,
    sample_rate_out: int,
    frame_ms: int,
    turn_limit: int,
    session_seconds: float,
    stt_provider: str,
    tts_provider: str,
    utterance_mode: str,
    continuation_pause_ms: float,
) -> dict[str, Any]:
    """The handshake. The client configures its worklet from this rather than
    hardcoding rates, so changing `app/voice/audio.py` never means shipping a
    matching browser change."""
    return {
        "type": "ready",
        "tenant_id": tenant_id,
        "tenant_name": tenant_name,
        "variant": variant,
        "sample_rate_in": sample_rate_in,
        "sample_rate_out": sample_rate_out,
        "frame_ms": frame_ms,
        "turn_limit": turn_limit,
        "session_seconds": session_seconds,
        "stt_provider": stt_provider,
        "tts_provider": tts_provider,
        "utterance_mode": utterance_mode,
        "continuation_pause_ms": continuation_pause_ms,
    }


def state(value: SessionState, turn: int) -> dict[str, Any]:
    return {"type": "state", "state": value, "turn": turn}


def transcript(text: str, *, final: bool, turn: int) -> dict[str, Any]:
    return {"type": "transcript", "text": text, "final": final, "turn": turn}


def token(text: str, turn: int) -> dict[str, Any]:
    """The reply as text, alongside the audio — so an operator can see what
    was said even when TTS is the fake (silent) provider."""
    return {"type": "token", "text": text, "turn": turn}


def metrics(turn: int, **timings: float | None) -> dict[str, Any]:
    """Per-turn latency. `first_audio_ms` is the §13 number — the one this
    whole phase exists to stop guessing at."""
    return {"type": "metrics", "turn": turn, **timings}


def error(message: str, *, turn: int) -> dict[str, Any]:
    """Recoverable: this turn died, the socket lives."""
    return {"type": "error", "message": message, "turn": turn}


def closing(reason: str) -> dict[str, Any]:
    """Terminal, and always sent before the close frame. A session that hits
    its cap must say so — going silent is indistinguishable from a bug, and
    this endpoint's caps exist precisely to be hit."""
    return {"type": "closing", "reason": reason}
