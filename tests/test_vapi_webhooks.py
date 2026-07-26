"""Vapi server events → `calls` records."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import reset_settings_cache
from app.db.memory_store import get_store
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def end_of_call(**overrides) -> dict:
    message = {
        "type": "end-of-call-report",
        "endedReason": "customer-ended-call",
        "startedAt": "2026-07-21T12:00:00.000Z",
        "endedAt": "2026-07-21T12:03:20.000Z",
        "durationSeconds": 200,
        "cost": 0.31,
        "transcript": "AI: Thanks for calling Hotel_MZV...\nUser: my lights are out",
        "recordingUrl": "https://example.invalid/recording.wav",
        "call": {
            "id": "call-xyz",
            "assistantId": "asst_hotelmzv_test",
            "customer": {"number": "+15557654321"},
        },
        "phoneNumber": {
            "number": "+15551230000",
            "twilioAccountSid": "AC_secret",
            "twilioAuthToken": "tok_secret",
        },
    }
    message.update(overrides)
    return {"message": message}


def test_end_of_call_report_is_persisted(client, hotel):
    response = client.post("/webhooks/vapi", json=end_of_call())

    assert response.status_code == 200
    assert response.json()["status"] == "recorded"

    calls = get_store().list_calls(hotel.tenant_id)
    assert len(calls) == 1

    call = calls[0]
    assert call.provider_call_id == "call-xyz"
    assert call.tenant_id == hotel.tenant_id
    assert call.from_number == "+15557654321"
    assert call.to_number == "+15551230000"
    assert call.duration_seconds == 200
    assert call.ended_reason == "customer-ended-call"
    assert call.cost_usd == pytest.approx(0.31)
    assert "my lights are out" in call.transcript
    assert call.started_at is not None and call.ended_at is not None
    assert call.channel == "voice"


def test_a_resent_report_updates_rather_than_duplicates(client, hotel):
    client.post("/webhooks/vapi", json=end_of_call())
    client.post("/webhooks/vapi", json=end_of_call(endedReason="assistant-ended-call"))

    calls = get_store().list_calls(hotel.tenant_id)
    assert len(calls) == 1, "webhook retries must not create duplicate call rows"
    assert calls[0].ended_reason == "assistant-ended-call"


def test_calls_are_scoped_to_their_tenant(client, hotel, northside):
    client.post("/webhooks/vapi", json=end_of_call())

    assert len(get_store().list_calls(hotel.tenant_id)) == 1
    assert get_store().list_calls(northside.tenant_id) == []


@pytest.mark.parametrize(
    "event_type",
    ["status-update", "transcript", "speech-update", "hang", "some-future-event"],
)
def test_unhandled_event_types_are_a_no_op_not_an_error(client, event_type):
    """4xx-ing these would show as delivery failures in Vapi's dashboard."""
    response = client.post("/webhooks/vapi", json={"message": {"type": event_type}})

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert get_store().list_calls("hotel-mzv") == []


def test_garbage_bodies_do_not_500(client):
    assert client.post("/webhooks/vapi", content=b"<xml/>").status_code == 200
    assert client.post("/webhooks/vapi", json={}).status_code == 200
    assert client.post("/webhooks/vapi", json={"message": {}}).status_code == 200


def test_report_for_an_unknown_tenant_is_dropped(client):
    body = end_of_call()
    body["message"]["call"]["assistantId"] = "asst_unknown"
    body["message"]["phoneNumber"]["number"] = "+19995550000"

    response = client.post("/webhooks/vapi", json=body)

    assert response.status_code == 200
    assert response.json()["status"] == "unmatched"
    assert get_store().list_calls("hotel-mzv") == []


def test_webhook_requires_the_secret_when_configured(client, monkeypatch):
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", "sh4red")
    reset_settings_cache()
    try:
        assert client.post("/webhooks/vapi", json=end_of_call()).status_code == 401
        assert (
            client.post(
                "/webhooks/vapi", json=end_of_call(), headers={"x-vapi-secret": "sh4red"}
            ).status_code
            == 200
        )
    finally:
        monkeypatch.delenv("VAPI_WEBHOOK_SECRET", raising=False)
        reset_settings_cache()


def test_unknown_tenant_log_does_not_leak_twilio_credentials(client, caplog):
    body = end_of_call()
    body["message"]["call"]["assistantId"] = "asst_unknown"
    body["message"]["phoneNumber"]["number"] = "+19995550000"
    body["message"]["call"]["twilioAuthToken"] = "tok_secret"

    with caplog.at_level("WARNING"):
        client.post("/webhooks/vapi", json=body)

    logged = json.dumps([r.getMessage() for r in caplog.records])
    assert "tok_secret" not in logged
