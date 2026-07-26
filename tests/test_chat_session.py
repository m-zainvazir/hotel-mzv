"""`POST /chat/session` — the public widget handshake (Phase 5)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.channels.widget_auth import verify_session_token
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_handshake_returns_tenant_display_data_and_services(client, hotel):
    response = client.post("/chat/session", json={"widget_key": "pk_widget_hotelmzv_demo"})

    assert response.status_code == 200
    body = response.json()
    assert body["tenant"]["name"] == hotel.name
    assert body["tenant"]["greeting"] == hotel.greeting
    assert body["tenant"]["accent_color"] == hotel.chat.accent_color
    assert body["tenant"]["quick_replies"] is True
    slugs = {s["slug"] for s in body["tenant"]["services"]}
    assert slugs == {s.slug for s in hotel.services}
    assert body["expires_in"] > 0
    assert body["session_id"]
    assert body["token"]


def test_handshake_token_verifies_to_the_right_tenant(client, hotel):
    response = client.post("/chat/session", json={"widget_key": "pk_widget_hotelmzv_demo"})
    body = response.json()

    claims = verify_session_token(body["token"])
    assert claims is not None
    assert claims.tenant_id == hotel.tenant_id
    assert claims.session_id == body["session_id"]


def test_unknown_widget_key_is_a_clean_404_not_a_broken_stream(client):
    response = client.post("/chat/session", json={"widget_key": "pk_does_not_exist"})
    assert response.status_code == 404


def test_two_handshakes_get_different_session_ids(client):
    first = client.post("/chat/session", json={"widget_key": "pk_widget_hotelmzv_demo"}).json()
    second = client.post("/chat/session", json={"widget_key": "pk_widget_hotelmzv_demo"}).json()
    assert first["session_id"] != second["session_id"]
    assert first["token"] != second["token"]


def test_empty_widget_key_is_rejected(client):
    assert client.post("/chat/session", json={"widget_key": ""}).status_code == 422


def test_origin_allowlist_is_bypassed_when_empty(client, hotel):
    assert hotel.chat.allowed_origins == []
    response = client.post(
        "/chat/session",
        json={"widget_key": "pk_widget_hotelmzv_demo"},
        headers={"Origin": "https://anything.example"},
    )
    assert response.status_code == 200


def test_origin_allowlist_rejects_a_disallowed_origin(client, override_tenant, hotel):
    tenant = hotel.model_copy(
        update={"chat": hotel.chat.model_copy(update={"allowed_origins": ["https://ok.example"]})}
    )
    override_tenant(tenant)

    rejected = client.post(
        "/chat/session",
        json={"widget_key": "pk_widget_hotelmzv_demo"},
        headers={"Origin": "https://evil.example"},
    )
    assert rejected.status_code == 403

    allowed = client.post(
        "/chat/session",
        json={"widget_key": "pk_widget_hotelmzv_demo"},
        headers={"Origin": "https://ok.example"},
    )
    assert allowed.status_code == 200


def test_origin_allowlist_rejects_a_missing_origin_header(client, override_tenant, hotel):
    tenant = hotel.model_copy(
        update={"chat": hotel.chat.model_copy(update={"allowed_origins": ["https://ok.example"]})}
    )
    override_tenant(tenant)

    response = client.post("/chat/session", json={"widget_key": "pk_widget_hotelmzv_demo"})
    assert response.status_code == 403
