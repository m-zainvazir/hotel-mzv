"""Durable chat transcripts — widget conversations write to the store
(Phase 5). Trusted/server-to-server callers have no handshake step and are
deliberately out of scope — see app/channels/chat.py's module docstring."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db.memory_store import get_store
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


def test_handshake_records_a_chat_session_row(client, hotel):
    session = _handshake(client)

    row = get_store().get_chat_session(hotel.tenant_id, session["session_id"])
    assert row is not None
    assert row.widget_key == "pk_widget_hotelmzv_demo"


def test_a_turn_records_both_the_user_and_assistant_messages(client, scripted, hotel):
    scripted(ai("Sure, checking now."))
    session = _handshake(client)

    response = client.post(
        "/chat",
        json={"message": "do you have a room tonight?"},
        headers={"Authorization": f"Bearer {session['token']}"},
    )
    assert response.status_code == 200

    messages = get_store().list_chat_messages(hotel.tenant_id, session["session_id"])
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "do you have a room tonight?"
    assert "checking now" in messages[1].content


def test_messages_from_different_sessions_never_mix(client, scripted, hotel):
    model = scripted(ai("first reply"), ai("second reply"))
    session_a = _handshake(client)
    session_b = _handshake(client)
    del model

    client.post(
        "/chat",
        json={"message": "message from a"},
        headers={"Authorization": f"Bearer {session_a['token']}"},
    )
    client.post(
        "/chat",
        json={"message": "message from b"},
        headers={"Authorization": f"Bearer {session_b['token']}"},
    )

    a_messages = get_store().list_chat_messages(hotel.tenant_id, session_a["session_id"])
    b_messages = get_store().list_chat_messages(hotel.tenant_id, session_b["session_id"])
    assert [m.content for m in a_messages if m.role == "user"] == ["message from a"]
    assert [m.content for m in b_messages if m.role == "user"] == ["message from b"]


def test_trusted_path_writes_no_transcript(client, scripted, hotel):
    """The trusted (bearer/body-driven) caller has no handshake-created
    ChatSession row, so it's deliberately excluded from persistence — see
    the module docstring in app/channels/chat.py."""
    scripted(ai("hello"))

    response = client.post(
        "/chat",
        json={"message": "hi", "tenant_id": hotel.tenant_id, "session_id": "trusted-session"},
    )
    assert response.status_code == 200

    assert get_store().list_chat_messages(hotel.tenant_id, "trusted-session") == []


def test_a_store_write_failure_never_breaks_the_stream(client, scripted, hotel, monkeypatch):
    scripted(ai("still works"))
    session = _handshake(client)

    async def _boom(self, message):
        raise RuntimeError("simulated store outage")

    monkeypatch.setattr(
        "app.db.memory_store.InMemoryStore.arecord_chat_message", _boom, raising=True
    )

    response = client.post(
        "/chat",
        json={"message": "hello"},
        headers={"Authorization": f"Bearer {session['token']}"},
    )

    assert response.status_code == 200
    payloads = _sse_payloads(response.text)
    assert payloads[-1]["type"] == "final"
    assert "still works" in payloads[-1]["text"]
