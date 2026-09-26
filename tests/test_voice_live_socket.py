"""`/voice/live` — the socket itself: who may open one, and what closes it.

Everything here drives the *real* route through `TestClient`, not the
orchestrator directly (`test_voice_turn.py` does that). The two halves a
route test can prove that an orchestrator test cannot: a token for the wrong
mode is refused, and a disabled channel never reaches the brain at all.
"""

from __future__ import annotations

import contextlib
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.channels.test_links import mint_test_token
from app.main import app
from app.tenancy.models import ChannelToggle
from app.voice import protocol
from app.voice.providers import set_voice_overrides
from app.voice.stt.fake import FakeSpeechToText
from app.voice.tts.fake import FakeTextToSpeech
from tests.conftest import ai


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def fake_voice_providers():
    set_voice_overrides(stt=FakeSpeechToText(), tts=FakeTextToSpeech())


@contextlib.contextmanager
def opened(client: TestClient, token: str):
    """Connect and authenticate, yielding `(socket, ready)`.

    A context manager rather than a plain helper because `TestClient`'s
    websocket must be exited on the same portal that opened it — leaving one
    unclosed hangs the *next* test's teardown, not this one's, which is a
    miserable failure to chase down.
    """
    with client.websocket_connect("/voice/live") as socket:
        socket.send_json({"type": "auth", "token": token})
        yield socket, socket.receive_json()


def test_a_voice_token_opens_a_socket_and_gets_a_ready_handshake(client, hotel):
    token = mint_test_token(hotel.tenant_id, mode="voice")
    with opened(client, token) as (socket, ready):
        assert ready["type"] == "ready"
        assert ready["tenant_id"] == hotel.tenant_id
        # The client configures its AudioWorklet from these rather than
        # hardcoding them, so changing app/voice/audio.py never means
        # shipping a matching browser change.
        assert ready["sample_rate_in"] == 16_000
        assert ready["sample_rate_out"] == 24_000
        assert socket.receive_json() == protocol.state("idle", 0)


def test_a_chat_token_cannot_open_a_voice_socket(client, hotel):
    """The mirror of `_resolve_test_mode` refusing a voice token on `/test/`.
    Two surfaces, neither accepting the other's token."""
    token = mint_test_token(hotel.tenant_id, mode="chat")
    with client.websocket_connect("/voice/live") as socket:
        socket.send_json({"type": "auth", "token": token})
        closing = socket.receive_json()
        assert closing["type"] == "closing"
        assert "not a voice test link" in closing["reason"]


def test_a_forged_token_is_refused(client):
    with client.websocket_connect("/voice/live") as socket:
        socket.send_json({"type": "auth", "token": "not.a.real.token"})
        assert socket.receive_json()["type"] == "closing"


def test_a_socket_with_no_auth_frame_at_all_is_refused(client, monkeypatch):
    monkeypatch.setenv("VOICE_AUTH_TIMEOUT_SECONDS", "0.05")
    from app.config import reset_settings_cache

    reset_settings_cache()
    with client.websocket_connect("/voice/live") as socket:
        closing = socket.receive_json()
        assert closing == protocol.closing("no auth message")


def test_a_malformed_auth_frame_is_refused_not_crashed(client):
    with client.websocket_connect("/voice/live") as socket:
        socket.send_text("{not json")
        assert socket.receive_json()["type"] == "closing"


def test_voice_disabled_for_a_tenant_refuses_the_socket(client, hotel, override_tenant):
    """The 9.1 channel flag, enforced at the handshake — so a turned-off bot
    can never reach the brain, let alone a metered STT socket."""
    override_tenant(
        hotel.model_copy(
            update={
                "channels": hotel.channels.model_copy(
                    # A raw dict here would sail past validation and land in
                    # the field as a dict — `model_copy` does not validate.
                    update={"voice": ChannelToggle(enabled=False)}
                )
            }
        )
    )
    token = mint_test_token(hotel.tenant_id, mode="voice")
    with client.websocket_connect("/voice/live") as socket:
        socket.send_json({"type": "auth", "token": token})
        closing = socket.receive_json()
        assert closing["type"] == "closing"
        assert "channel 'voice' disabled" in closing["reason"]


def test_voice_live_disabled_deployment_wide_refuses_the_handshake(client, hotel, monkeypatch):
    monkeypatch.setenv("VOICE_LIVE_ENABLED", "false")
    from app.config import reset_settings_cache

    reset_settings_cache()
    token = mint_test_token(hotel.tenant_id, mode="voice")
    # Closed before `accept()`, so this is a rejected handshake rather than an
    # accepted-then-dropped socket — the WebSocket equivalent of the 404
    # `ADMIN_ENABLED=false` gives.
    with pytest.raises((WebSocketDisconnect, Exception)):
        with client.websocket_connect("/voice/live") as socket:
            socket.send_json({"type": "auth", "token": token})
            socket.receive_json()


def test_an_unknown_tenant_is_refused(client):
    token = mint_test_token("no-such-bot", mode="voice")
    with client.websocket_connect("/voice/live") as socket:
        socket.send_json({"type": "auth", "token": token})
        assert socket.receive_json() == protocol.closing("unknown tenant")


def test_the_turn_cap_closes_the_session_with_a_reason(client, hotel, monkeypatch, scripted):
    """A cap that goes silent is indistinguishable from a crash, and these
    caps exist precisely to be hit."""
    monkeypatch.setenv("VOICE_MAX_TURNS_PER_SESSION", "1")
    from app.config import reset_settings_cache

    reset_settings_cache()
    scripted(ai("Hi."), ai("Hi again."))

    token = mint_test_token(hotel.tenant_id, mode="voice")
    with opened(client, token) as (socket, _):
        socket.send_json({"type": "text", "text": "hello"})
        _drain_until(socket, "metrics")
        socket.send_json({"type": "text", "text": "hello again"})
        closing = _drain_until(socket, "closing")
        assert closing["reason"] == "turn limit reached"


def test_unknown_control_frames_are_ignored_not_fatal(client, hotel):
    """Attacker-reachable input: one bad frame from a flaky browser must
    never end a live session."""
    token = mint_test_token(hotel.tenant_id, mode="voice")
    with opened(client, token) as (socket, _):
        socket.receive_json()  # the initial idle state
        socket.send_json({"type": "nonsense", "wat": 1})
        socket.send_text("not json at all")
        socket.send_json({"type": "cancel"})
        # Still alive and answering.
        assert socket.receive_json() == protocol.state("idle", 0)


def test_audio_before_the_floor_is_taken_is_dropped(client, hotel):
    """A client that streams without `start_utterance` must not be able to
    push bytes into a metered STT socket."""
    stt = FakeSpeechToText()
    set_voice_overrides(stt=stt, tts=FakeTextToSpeech())
    token = mint_test_token(hotel.tenant_id, mode="voice")
    with opened(client, token) as (socket, _):
        socket.receive_json()
        socket.send_bytes(bytes(640))
        socket.send_json({"type": "cancel"})
        socket.receive_json()
        assert stt.sessions == []


def _drain_until(socket, kind: str, limit: int = 60) -> dict:
    for _ in range(limit):
        message = socket.receive()
        if message.get("text") is None:
            continue
        payload = json.loads(message["text"])
        if payload.get("type") == kind:
            return payload
    raise AssertionError(f"never saw a {kind!r} message")


# --- the page that drives the socket ---------------------------------------


def test_a_voice_link_serves_the_voice_tester_page(client, hotel):
    """9.1 minted `mode: "voice"` tokens and 404'd them. They work now — the
    same signed link, a different page behind it."""
    token = mint_test_token(hotel.tenant_id, mode="voice")
    response = client.get(f"/test/{token}")

    assert response.status_code == 200
    assert "Voice tester" in response.text
    assert "/voice/live" in response.text
    assert "pcm-capture" in response.text  # the AudioWorklet is really there
    assert token in response.text


def test_a_chat_link_still_serves_the_widget_page(client, hotel):
    token = mint_test_token(hotel.tenant_id, mode="chat")
    response = client.get(f"/test/{token}")

    assert response.status_code == 200
    assert "/widget.js" in response.text
    assert "/voice/live" not in response.text


def test_a_draft_voice_link_keeps_the_amber_accent(client, hotel):
    """Losing the draft/live signal on a second surface would be worse than
    any styling win."""
    token = mint_test_token(hotel.tenant_id, mode="voice", variant="draft")
    response = client.get(f"/test/{token}")

    assert "Voice draft preview" in response.text
    assert "#fbbf24" in response.text


def test_a_voice_link_cannot_mint_a_chat_session(client, hotel):
    """`/test/session` is the *chat* handshake. Minting one from a voice link
    would hand the tester a token for the wrong channel entirely."""
    token = mint_test_token(hotel.tenant_id, mode="voice")
    response = client.post("/test/session", json={"token": token})

    assert response.status_code == 404
    assert "not a chat test link" in response.json()["detail"]


def test_the_voice_page_is_refused_when_the_relay_is_turned_off(client, hotel, monkeypatch):
    monkeypatch.setenv("VOICE_LIVE_ENABLED", "false")
    from app.config import reset_settings_cache

    reset_settings_cache()
    token = mint_test_token(hotel.tenant_id, mode="voice")

    assert client.get(f"/test/{token}").status_code == 404


def test_a_voice_link_works_on_a_bot_with_chat_turned_off(client, hotel, override_tenant):
    """The channel checked is the one the *link* is for. A voice-only bot is
    a legitimate configuration, and the shared resolver used to hardcode chat."""
    override_tenant(
        hotel.model_copy(
            update={
                "channels": hotel.channels.model_copy(update={"chat": ChannelToggle(enabled=False)})
            }
        )
    )
    token = mint_test_token(hotel.tenant_id, mode="voice")

    assert client.get(f"/test/{token}").status_code == 200


def test_every_message_type_the_session_handles_survives_the_route(client, hotel):
    """The route drops unknown types before the session ever sees them, so a
    type added to `VoiceSession.handle` and not to `ClientMessage.KNOWN` is
    inert over a real socket while every direct-call test still passes.
    That is exactly how `mic_on`/`mic_off` shipped broken for one live run:
    the mic button sent a frame nothing was listening for."""
    import inspect

    from app.voice import session as session_module
    from app.voice.protocol import ClientMessage

    handled = {
        literal
        for literal in inspect.getsource(session_module.VoiceSession.handle).split('"')
        if literal in ("mic_on", "mic_off", "start_utterance", "end_utterance", "cancel", "text")
    }
    assert handled, "could not read the handled types"
    missing = handled - set(ClientMessage.KNOWN)
    assert not missing, f"{missing} would be dropped by the route"


def test_the_mic_switch_reaches_the_session_over_a_real_socket(client, hotel):
    token = mint_test_token(hotel.tenant_id, mode="voice")
    with opened(client, token) as (socket, _):
        assert socket.receive_json() == protocol.state("idle", 0)
        socket.send_json({"type": "mic_on"})
        # Listening means the mic frame was understood — the failure it
        # guards against is total silence here.
        assert socket.receive_json() == protocol.state("listening", 0)
        socket.send_json({"type": "mic_off"})
