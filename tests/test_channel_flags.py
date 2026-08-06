"""`TenantConfig.channels` + `require_channel_enabled` (Phase 9.1).

Defaults are true/true, so every OTHER test in the suite that never touches
`channels` is itself proof "defaults change nothing" — this file only
exercises the flag actually being off.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.tenancy import loader
from app.tenancy.loader import require_channel_enabled
from app.tenancy.models import ChannelSettings, ChannelToggle
from app.tenancy.repository import ChannelDisabledError, TenantNotFoundError
from tests.conftest import ai

FIXTURE = Path(__file__).parent / "fixtures" / "vapi_chat_completion_request.json"


class _StaticRepo:
    """A repository serving exactly one tenant on every lookup path — same
    shape as test_admin_tenant_crud.py's own fixture of the same name, kept
    local per this codebase's one-file-per-concern test convention."""

    def __init__(self, tenant) -> None:
        self.tenant = tenant

    def get(self, tenant_id: str):
        if tenant_id == self.tenant.tenant_id:
            return self.tenant
        raise TenantNotFoundError(tenant_id)

    def list_ids(self) -> list[str]:
        return [self.tenant.tenant_id]

    def find_by_phone(self, phone_number: str):
        return self.tenant if phone_number in self.tenant.phone_numbers else None

    def find_by_widget_key(self, widget_key: str):
        return self.tenant if widget_key in self.tenant.widget_keys else None

    def find_by_assistant_id(self, assistant_id: str):
        return None


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


class TestChannelSettingsDefaults:
    def test_both_channels_default_enabled(self):
        settings = ChannelSettings()
        assert settings.chat.enabled is True
        assert settings.voice.enabled is True

    def test_a_fresh_tenant_carries_the_defaults(self, hotel):
        assert hotel.channels.chat.enabled is True
        assert hotel.channels.voice.enabled is True


class TestRequireChannelEnabled:
    def test_enabled_channel_is_a_no_op(self, hotel):
        require_channel_enabled(hotel, "chat")
        require_channel_enabled(hotel, "voice")

    def test_disabled_chat_raises(self, hotel):
        tenant = hotel.model_copy(
            update={"channels": ChannelSettings(chat=ChannelToggle(enabled=False))}
        )
        with pytest.raises(ChannelDisabledError):
            require_channel_enabled(tenant, "chat")

    def test_disabled_voice_raises(self, hotel):
        tenant = hotel.model_copy(
            update={"channels": ChannelSettings(voice=ChannelToggle(enabled=False))}
        )
        with pytest.raises(ChannelDisabledError):
            require_channel_enabled(tenant, "voice")

    def test_disabling_one_channel_never_touches_the_other(self, hotel):
        tenant = hotel.model_copy(
            update={"channels": ChannelSettings(chat=ChannelToggle(enabled=False))}
        )
        require_channel_enabled(tenant, "voice")  # does not raise

    def test_channel_disabled_error_subclasses_tenant_not_found(self):
        assert issubclass(ChannelDisabledError, TenantNotFoundError)


class TestChatSessionRefusesDisabledChat:
    def test_chat_disabled_404s_the_widget_handshake(self, client, hotel):
        disabled = hotel.model_copy(
            update={"channels": ChannelSettings(chat=ChannelToggle(enabled=False))}
        )
        loader.set_repository(_StaticRepo(disabled))
        try:
            response = client.post("/chat/session", json={"widget_key": disabled.widget_keys[0]})
        finally:
            loader.set_repository(None)
        assert response.status_code == 404

    def test_chat_enabled_by_default_still_works(self, client, hotel):
        loader.set_repository(_StaticRepo(hotel))
        try:
            response = client.post("/chat/session", json={"widget_key": hotel.widget_keys[0]})
        finally:
            loader.set_repository(None)
        assert response.status_code == 200


class TestVapiCompletionsRefusesDisabledVoice:
    def test_voice_disabled_404s_a_vapi_turn(self, client, hotel):
        """Exercises the phone-number fallback path, same as a live call
        would — the fixture's assistantId doesn't match any real tenant
        (mirrors test_admin_tenant_crud.py's archived-tenant equivalent)."""
        disabled = hotel.model_copy(
            update={"channels": ChannelSettings(voice=ChannelToggle(enabled=False))}
        )
        loader.set_repository(_StaticRepo(disabled))
        try:
            payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
            payload.pop("_comment", None)
            response = client.post("/chat/completions", json=payload)
        finally:
            loader.set_repository(None)
        assert response.status_code == 404

    def test_voice_enabled_by_default_still_works(self, client, hotel, scripted):
        scripted(ai("hello"))
        loader.set_repository(_StaticRepo(hotel))
        try:
            payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
            payload.pop("_comment", None)
            response = client.post("/chat/completions", json=payload)
        finally:
            loader.set_repository(None)
        assert response.status_code == 200


class TestChatRouteTrustedPathRefusesDisabledChat:
    def test_disabled_chat_404s_the_trusted_body_driven_path(self, client, hotel):
        disabled = hotel.model_copy(
            update={"channels": ChannelSettings(chat=ChannelToggle(enabled=False))}
        )
        loader.set_repository(_StaticRepo(disabled))
        try:
            response = client.post(
                "/chat",
                json={"message": "hi", "tenant_id": disabled.tenant_id, "session_id": "s"},
            )
        finally:
            loader.set_repository(None)
        assert response.status_code == 404
