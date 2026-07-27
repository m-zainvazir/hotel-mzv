"""Phase 7 Step 6 — the in-process rate limiter: window accounting, the 429
shape, per-session independence, and the routes that must never be limited."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.channels.ratelimit as ratelimit
from app.config import get_settings, reset_settings_cache
from app.main import app
from tests.conftest import ai

VAPI_FIXTURE = Path(__file__).parent / "fixtures" / "vapi_chat_completion_request.json"


def _vapi_payload(**overrides) -> dict:
    body = json.loads(VAPI_FIXTURE.read_text(encoding="utf-8"))
    body.pop("_comment", None)
    body.update(overrides)
    return body


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


def _fake_clock(monkeypatch, start: float = 1000.0):
    box = {"now": start}
    monkeypatch.setattr(ratelimit, "_monotonic", lambda: box["now"])
    return box


# --- window accounting, against an injected clock ---------------------------


def test_hit_allows_up_to_the_limit_then_blocks(monkeypatch):
    _fake_clock(monkeypatch)
    for _ in range(5):
        assert ratelimit._hit("scope", "key", limit=5, window_seconds=60.0) is None
    retry_after = ratelimit._hit("scope", "key", limit=5, window_seconds=60.0)
    assert retry_after is not None
    assert 0 < retry_after <= 60.0


def test_hit_resets_once_the_window_elapses(monkeypatch):
    clock = _fake_clock(monkeypatch)
    for _ in range(5):
        assert ratelimit._hit("scope", "key", limit=5, window_seconds=60.0) is None
    assert ratelimit._hit("scope", "key", limit=5, window_seconds=60.0) is not None

    clock["now"] += 61.0
    assert ratelimit._hit("scope", "key", limit=5, window_seconds=60.0) is None


def test_hit_scopes_are_independent(monkeypatch):
    _fake_clock(monkeypatch)
    for _ in range(3):
        assert ratelimit._hit("scope-a", "key", limit=3, window_seconds=60.0) is None
    # A different scope, same key, starts fresh.
    assert ratelimit._hit("scope-b", "key", limit=3, window_seconds=60.0) is None


# --- the 429 shape ------------------------------------------------------------


def test_429_carries_a_retry_after_header(client, scripted, hotel):
    scripted(*[ai("reply") for _ in range(100)])
    session = _handshake(client)
    headers = {"Authorization": f"Bearer {session['token']}"}

    # The handshake above already spent one hit of the shared per-IP budget
    # (chat_requests_per_minute is shared across /chat/session and /chat by
    # design -- see app/channels/ratelimit.py's docstring), so only
    # limit - 1 further /chat calls fit before the next one is blocked.
    limit = get_settings().chat_requests_per_minute
    for _ in range(limit - 1):
        response = client.post("/chat", json={"message": "hi"}, headers=headers)
        assert response.status_code == 200

    blocked = client.post("/chat", json={"message": "hi"}, headers=headers)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


def test_rate_limiting_can_be_disabled(client, monkeypatch, scripted, hotel):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    reset_settings_cache()
    try:
        scripted(*[ai("reply") for _ in range(100)])
        session = _handshake(client)
        headers = {"Authorization": f"Bearer {session['token']}"}
        for _ in range(get_settings().chat_requests_per_minute + 5):
            response = client.post("/chat", json={"message": "hi"}, headers=headers)
            assert response.status_code == 200
    finally:
        reset_settings_cache()


# --- per-session independence -------------------------------------------------


def test_one_widget_session_hitting_the_limit_does_not_affect_another(
    client, scripted, hotel, monkeypatch
):
    # Isolate the session-scoped limiter: bump the per-IP burst cap so it
    # never trips first and masks what this test is actually checking.
    monkeypatch.setenv("CHAT_REQUESTS_PER_MINUTE", "100000")
    reset_settings_cache()
    try:
        scripted(*[ai("reply") for _ in range(200)])
        session_a = _handshake(client)
        session_b = _handshake(client)
        headers_a = {"Authorization": f"Bearer {session_a['token']}"}
        headers_b = {"Authorization": f"Bearer {session_b['token']}"}

        limit = get_settings().session_requests_per_hour
        for _ in range(limit):
            response = client.post("/chat", json={"message": "hi"}, headers=headers_a)
            assert response.status_code == 200

        assert client.post("/chat", json={"message": "hi"}, headers=headers_a).status_code == 429
        # A different session is untouched.
        assert client.post("/chat", json={"message": "hi"}, headers=headers_b).status_code == 200
    finally:
        reset_settings_cache()


def test_trusted_caller_is_exempt_from_rate_limiting(client, monkeypatch, scripted, hotel):
    monkeypatch.setenv("API_AUTH_TOKEN", "tok")
    reset_settings_cache()
    try:
        scripted(*[ai("reply") for _ in range(200)])
        headers = {"Authorization": "Bearer tok"}
        limit = get_settings().chat_requests_per_minute
        for _ in range(limit + 5):
            response = client.post(
                "/chat",
                json={"message": "hi", "tenant_id": hotel.tenant_id},
                headers=headers,
            )
            assert response.status_code == 200
    finally:
        reset_settings_cache()


# --- routes that must never be limited ----------------------------------------


def test_vapi_completions_is_never_rate_limited(client, scripted, hotel):
    limit = get_settings().chat_requests_per_minute
    scripted(*[ai("reply") for _ in range(limit + 5)])
    for _ in range(limit + 5):
        response = client.post("/chat/completions", json=_vapi_payload())
        # No rate-limit dependency is attached to this route at all -- the
        # point here is purely that none of these responses is ever a 429.
        assert response.status_code != 429
