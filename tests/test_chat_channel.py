"""`POST /chat` with a widget session token (Phase 5).

Handshake behaviour lives in test_chat_session.py; this covers the two
callers `require_chat_caller` distinguishes and the event filtering that
makes `/chat` safe to expose to a browser.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import reset_settings_cache
from app.main import app
from tests.conftest import ai


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _sse_payloads(body: str) -> list[dict]:
    events = []
    for line in body.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[6:]))
    return events


def _handshake(client, widget_key: str = "pk_widget_hotelmzv_demo") -> dict:
    response = client.post("/chat/session", json={"widget_key": widget_key})
    assert response.status_code == 200
    return response.json()


def test_widget_token_drives_the_turn(client, scripted, hotel):
    scripted(ai("Hotel_MZV, how can I help?"))
    session = _handshake(client)

    response = client.post(
        "/chat",
        json={"message": "hello"},
        headers={"Authorization": f"Bearer {session['token']}"},
    )

    assert response.status_code == 200
    payloads = _sse_payloads(response.text)
    assert payloads[-1]["type"] == "final"


def test_widget_token_tenant_wins_over_a_body_tenant_id(client, scripted, hotel, northside):
    model = scripted(ai("reply"))
    session = _handshake(client, "pk_widget_hotelmzv_demo")

    response = client.post(
        "/chat",
        json={"message": "hello", "tenant_id": northside.tenant_id},
        headers={"Authorization": f"Bearer {session['token']}"},
    )

    assert response.status_code == 200
    system_prompt = str(model.seen_prompts[0][0].content)
    assert hotel.name in system_prompt
    assert northside.name not in system_prompt


def test_two_widget_sessions_never_share_a_thread(client, scripted, hotel):
    model = scripted(ai("first"), ai("second"))
    session_a = _handshake(client)
    session_b = _handshake(client)

    assert session_a["session_id"] != session_b["session_id"]

    client.post(
        "/chat",
        json={"message": "hi from a"},
        headers={"Authorization": f"Bearer {session_a['token']}"},
    )
    client.post(
        "/chat",
        json={"message": "hi from b"},
        headers={"Authorization": f"Bearer {session_b['token']}"},
    )

    # Different session ids from the handshake must land on different
    # checkpointer threads — b's prompt must not carry a's message.
    second_prompt_text = " ".join(str(m.content) for m in model.seen_prompts[1])
    assert "hi from a" not in second_prompt_text
    assert "hi from b" in second_prompt_text


def test_suggestions_event_carries_slots(client, scripted, hotel):
    scripted(
        ai(
            "Let me look. ",
            [{"name": "check_availability", "args": {"service": "room-reservation"}}],
        ),
        ai("Here you go."),
    )
    session = _handshake(client)

    response = client.post(
        "/chat",
        json={"message": "room for tonight?"},
        headers={"Authorization": f"Bearer {session['token']}"},
    )

    payloads = _sse_payloads(response.text)
    suggestions = [p for p in payloads if p["type"] == "suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["data"]["kind"] == "slots"
    assert suggestions[0]["data"]["service"] == "room-reservation"
    assert suggestions[0]["data"]["slots"], "expected at least one slot"
    assert "start_iso" in suggestions[0]["data"]["slots"][0]
    assert "label" in suggestions[0]["data"]["slots"][0]


def test_handoff_event_reaches_the_widget_client(client, scripted, hotel):
    scripted(
        ai("That sounds serious. ", [{"name": "escalate", "args": {"reason": "gas smell"}}]),
        ai("Please call our on-call number."),
    )
    session = _handshake(client)

    response = client.post(
        "/chat",
        json={"message": "I smell gas"},
        headers={"Authorization": f"Bearer {session['token']}"},
    )

    payloads = _sse_payloads(response.text)
    handoffs = [p for p in payloads if p["type"] == "handoff"]
    assert len(handoffs) == 1
    assert handoffs[0]["data"]["transfer"] is False


def test_tool_events_never_reach_the_widget_client(client, scripted, hotel):
    scripted(
        ai("Let me look. ", [{"name": "check_availability", "args": {"service": "diagnostic"}}]),
        ai("Here's what I found."),
    )
    session = _handshake(client)

    response = client.post(
        "/chat",
        json={"message": "book me a room"},
        headers={"Authorization": f"Bearer {session['token']}"},
    )

    payloads = _sse_payloads(response.text)
    assert not any(p["type"] in ("tool_start", "tool_result") for p in payloads)
    assert payloads[-1]["type"] == "final"


def test_a_garbage_bearer_is_rejected_when_a_shared_secret_is_configured(client, monkeypatch):
    monkeypatch.setenv("API_AUTH_TOKEN", "s3cret")
    reset_settings_cache()
    try:
        response = client.post(
            "/chat",
            json={"message": "hi", "tenant_id": "hotel-mzv"},
            headers={"Authorization": "Bearer not-a-real-token-or-secret"},
        )
        assert response.status_code == 401
    finally:
        monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
        reset_settings_cache()


def test_widget_token_still_works_when_a_shared_secret_is_configured(client, scripted, monkeypatch):
    """The widget path and the trusted bearer path are independent schemes —
    configuring API_AUTH_TOKEN for server-to-server callers must not lock out
    a widget session token."""
    monkeypatch.setenv("API_AUTH_TOKEN", "s3cret")
    reset_settings_cache()
    try:
        scripted(ai("hello"))
        session = _handshake(client)

        response = client.post(
            "/chat",
            json={"message": "hi"},
            headers={"Authorization": f"Bearer {session['token']}"},
        )
        assert response.status_code == 200
    finally:
        monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
        reset_settings_cache()


def test_heartbeats_dont_corrupt_sse_framing(client, scripted, hotel, monkeypatch):
    import app.channels.chat as chat_module

    monkeypatch.setattr(chat_module, "HEARTBEAT_INTERVAL_SECONDS", 0.0001)
    scripted(ai("hello there"))
    session = _handshake(client)

    response = client.post(
        "/chat",
        json={"message": "hi"},
        headers={"Authorization": f"Bearer {session['token']}"},
    )

    assert response.status_code == 200
    payloads = _sse_payloads(response.text)
    assert payloads[-1]["type"] == "final"
    assert response.text.rstrip().endswith("data: [DONE]")
