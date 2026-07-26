"""TwilioNotifier: wire format, error mapping, and the escalate-survives-a-
failed-alert guarantee (app/tools/messaging_tools.py::escalate)."""

from __future__ import annotations

import base64

import httpx
import pytest

from app.config import Settings
from app.db.memory_store import get_store
from app.tools.messaging.base import MessagingError
from app.tools.messaging.twilio import TwilioNotifier
from app.tools.messaging_tools import escalate
from app.tools.providers import reset_provider_overrides, set_booking_provider
from tests.conftest import mock_http, tool_config


def _settings(**overrides) -> Settings:
    base = {
        "twilio_account_sid": "ACtest",
        "twilio_auth_token": "secrettoken",
        "twilio_from_number": "+15005550006",
    }
    base.update(overrides)
    return Settings(**base)


def _ok(sid: str = "SMxxxx", status: str = "queued") -> httpx.Response:
    return httpx.Response(201, json={"sid": sid, "status": status})


async def test_sends_form_encoded_body_with_basic_auth(hotel):
    client, requests = mock_http(lambda req: _ok(), auth=("ACtest", "secrettoken"))
    notifier = TwilioNotifier(client=client, settings=_settings())

    message = await notifier.send_sms(
        hotel, to="+15551234567", body="hi there", kind="confirmation"
    )

    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/2010-04-01/Accounts/ACtest/Messages.json"
    auth_header = request.headers["authorization"]
    assert auth_header.startswith("Basic ")
    decoded = base64.b64decode(auth_header.removeprefix("Basic ")).decode()
    assert decoded == "ACtest:secrettoken"

    form = dict(httpx.QueryParams(request.content.decode()))
    assert form["To"] == "+15551234567"
    assert form["Body"] == "hi there"
    # hotel-mzv's notifications.from_number takes precedence over settings.
    assert form["From"] == hotel.notifications.from_number

    assert message.provider == "twilio"
    assert message.provider_sid == "SMxxxx"
    assert message.status == "queued"


async def test_auto_built_client_carries_basic_auth_from_settings():
    """No client= injected: the notifier must build one with (sid, token) auth
    itself. No request is sent, so this needs no mock transport."""
    notifier = TwilioNotifier(settings=_settings())
    client, sid = await notifier._get_client("hotel-mzv")
    assert client.auth is not None
    assert sid == _settings().twilio_account_sid


async def test_messaging_service_sid_replaces_from(hotel):
    client, requests = mock_http(lambda req: _ok())
    settings = _settings(twilio_messaging_service_sid="MGabc123")
    notifier = TwilioNotifier(client=client, settings=settings)

    await notifier.send_sms(hotel, to="+15551234567", body="hi", kind="confirmation")

    form = dict(httpx.QueryParams(requests[0].content.decode()))
    assert form["MessagingServiceSid"] == "MGabc123"
    assert "From" not in form


async def test_tenant_from_number_preferred_over_settings(hotel):
    client, requests = mock_http(lambda req: _ok())
    notifier = TwilioNotifier(client=client, settings=_settings())

    # hotel-mzv's notifications.from_number is +15551230000 in the seed tenant.
    await notifier.send_sms(hotel, to="+15551234567", body="hi", kind="confirmation")

    form = dict(httpx.QueryParams(requests[0].content.decode()))
    assert form["From"] == hotel.notifications.from_number


async def test_missing_credentials_raise_without_any_http_call(hotel):
    client, requests = mock_http(lambda req: _ok())
    notifier = TwilioNotifier(client=client, settings=_settings(twilio_account_sid=None))

    with pytest.raises(MessagingError):
        await notifier.send_sms(hotel, to="+15551234567", body="hi", kind="confirmation")

    assert requests == []


async def test_missing_sender_raises_without_http_call(hotel):
    tenant = hotel.model_copy(
        update={"notifications": hotel.notifications.model_copy(update={"from_number": None})}
    )
    client, requests = mock_http(lambda req: _ok())
    notifier = TwilioNotifier(client=client, settings=_settings(twilio_from_number=None))

    with pytest.raises(MessagingError):
        await notifier.send_sms(tenant, to="+15551234567", body="hi", kind="confirmation")

    assert requests == []


async def test_4xx_raises_messaging_error_and_records_a_failed_row(hotel):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 21211, "message": "Invalid 'To' Phone Number"})

    client, _requests = mock_http(handler)
    notifier = TwilioNotifier(client=client, settings=_settings())

    with pytest.raises(MessagingError, match="21211"):
        await notifier.send_sms(hotel, to="+1555", body="hi", kind="alert")

    rows = get_store().list_messages(hotel.tenant_id)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert "21211" in rows[0].error


async def test_transport_error_raises_messaging_error(hotel):
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client, _requests = mock_http(handler)
    notifier = TwilioNotifier(client=client, settings=_settings())

    with pytest.raises(MessagingError):
        await notifier.send_sms(hotel, to="+15551234567", body="hi", kind="confirmation")


async def test_escalate_still_succeeds_when_the_alert_sms_raises(hotel, scripted):
    """The single most important guarantee: a failed Twilio alert must never
    take the escalation down with it (app/tools/messaging_tools.py::escalate)."""

    class FailingNotifier:
        name = "twilio"

        async def send_sms(self, *_args, **_kwargs):
            raise MessagingError("twilio error: 21211: Invalid 'To' Phone Number")

    from app.tools import providers

    providers._notifier_override = FailingNotifier()
    try:
        result = await escalate.ainvoke(
            {"reason": "gas smell", "caller_summary": "Jane at 123 Main St"},
            config=tool_config(hotel.tenant_id, channel="chat"),
        )
    finally:
        reset_provider_overrides()
        set_booking_provider(hotel.tenant_id, None)

    assert "ESCALATED" in result
    assert len(get_store().list_escalations(hotel.tenant_id)) == 1
