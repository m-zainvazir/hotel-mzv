"""`GET /admin/api/tenants/{id}/calcom` — Phase 9.4.

The route the Config tab reads to decide whether to show an editable hours
grid or Cal.com's read-only schedule. Getting this wrong in the "connected"
direction is the expensive one: the panel would hide the only controls that
actually work.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.channels.admin as admin_module
from app.config import reset_settings_cache
from app.main import app
from app.tenancy.models import BookingSettings
from app.tools.booking.base import AvailabilitySchedule, ScheduleWindow

_TOKEN = "s3cret-admin-token-that-is-long-enough-to-pass-preflight"
_KARACHI_SCHEDULE = AvailabilitySchedule(
    windows=[ScheduleWindow(days=["Monday", "Tuesday"], start="09:00", end="17:00")],
    timezone="Asia/Karachi",
    name="Front desk",
    source="calcom",
)


def _bearer() -> dict:
    return {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture
def calcom_client(monkeypatch):
    monkeypatch.setenv("ADMIN_ENABLED", "true")
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TOKEN)
    reset_settings_cache()

    async def _no_draft(tenant_id, *, client=None):
        return None, None

    monkeypatch.setattr(admin_module.tenancy_admin, "get_draft", _no_draft)
    with TestClient(app) as test_client:
        yield test_client
    reset_settings_cache()


def _configure(monkeypatch, *, booking, schedule=None, grant=True, api_key="k"):
    """Point the route at a chosen provider config and credential state."""
    from app.tenancy import loader

    original = loader.get_tenant_config

    def _patched(tenant_id: str):
        return original(tenant_id).model_copy(update={"booking": booking})

    monkeypatch.setattr(admin_module, "get_tenant_config", _patched)

    async def _schedule(config, *, refresh=False):
        return schedule

    async def _grant(tenant_id):
        return grant

    async def _secret(tenant_id, key, env_value=None):
        return api_key

    monkeypatch.setattr(admin_module, "availability_schedule_for", _schedule)
    monkeypatch.setattr(admin_module, "has_calcom_grant", _grant)
    monkeypatch.setattr(admin_module, "resolve_secret", _secret)


class TestNotConnected:
    def test_a_stub_tenant_is_told_its_hours_are_its_own(self, calcom_client, monkeypatch):
        _configure(monkeypatch, booking=BookingSettings(provider="stub"))
        body = calcom_client.get("/admin/api/tenants/hotel-mzv/calcom", headers=_bearer()).json()

        assert body["connected"] is False
        assert body["provider"] == "stub"
        assert "isn't connected to a calendar" in body["reason"]

    def test_calcom_with_no_event_type_is_not_connected(self, calcom_client, monkeypatch):
        _configure(monkeypatch, booking=BookingSettings(provider="calcom", event_type_id=None))
        body = calcom_client.get("/admin/api/tenants/hotel-mzv/calcom", headers=_bearer()).json()

        assert body["connected"] is False
        assert "no event type" in body["reason"]

    def test_mcp_with_no_grant_names_the_exact_command(self, calcom_client, monkeypatch):
        """A reason that doesn't tell you what to type is a dead end — the
        same convention the voice-consent 409 already follows."""
        _configure(
            monkeypatch,
            booking=BookingSettings(provider="mcp_calcom", event_type_id=7),
            grant=False,
        )
        body = calcom_client.get("/admin/api/tenants/hotel-mzv/calcom", headers=_bearer()).json()

        assert body["connected"] is False
        assert "scripts.authorize_calcom --tenant hotel-mzv" in body["reason"]

    def test_calcom_with_no_api_key_is_not_connected(self, calcom_client, monkeypatch):
        _configure(
            monkeypatch,
            booking=BookingSettings(provider="calcom", event_type_id=7),
            api_key=None,
        )
        body = calcom_client.get("/admin/api/tenants/hotel-mzv/calcom", headers=_bearer()).json()

        assert body["connected"] is False
        assert "No Cal.com API key" in body["reason"]

    def test_connected_but_no_schedule_says_so_rather_than_claiming_hours(
        self, calcom_client, monkeypatch
    ):
        _configure(
            monkeypatch,
            booking=BookingSettings(provider="calcom", event_type_id=7),
            schedule=None,
        )
        body = calcom_client.get("/admin/api/tenants/hotel-mzv/calcom", headers=_bearer()).json()

        assert body["connected"] is True
        assert body["schedule"] is None
        assert "didn't return a schedule" in body["reason"]


class TestConnected:
    def test_returns_the_live_schedule(self, calcom_client, monkeypatch):
        _configure(
            monkeypatch,
            booking=BookingSettings(provider="mcp_calcom", event_type_id=7),
            schedule=_KARACHI_SCHEDULE,
        )
        body = calcom_client.get("/admin/api/tenants/hotel-mzv/calcom", headers=_bearer()).json()

        assert body["connected"] is True
        assert body["reason"] is None
        assert body["event_type_id"] == 7
        assert body["schedule"]["windows"][0]["days"] == ["Monday", "Tuesday"]
        assert body["timezone"] == "Asia/Karachi"

    def test_timezone_drift_is_reported(self, calcom_client, monkeypatch):
        """The bug this exists for: a Karachi account displayed a New-York
        8pm booking as next-day 5am, because the two timezones disagreed."""
        drifted = _KARACHI_SCHEDULE.model_copy(update={"timezone": "America/New_York"})
        _configure(
            monkeypatch,
            booking=BookingSettings(provider="calcom", event_type_id=7),
            schedule=drifted,
        )
        body = calcom_client.get("/admin/api/tenants/hotel-mzv/calcom", headers=_bearer()).json()

        assert body["connected"] is True
        assert body["timezone_matches"] is False

    def test_matching_timezones_do_not_warn(self, calcom_client, monkeypatch):
        _configure(
            monkeypatch,
            booking=BookingSettings(provider="calcom", event_type_id=7),
            schedule=_KARACHI_SCHEDULE,
        )
        body = calcom_client.get("/admin/api/tenants/hotel-mzv/calcom", headers=_bearer()).json()
        assert body["timezone_matches"] is True

    def test_the_panel_bypasses_the_schedule_cache(self, calcom_client, monkeypatch):
        """An operator opening this panel usually just edited Cal.com. A
        15-minute-old cached answer would make the panel look broken."""
        seen: list[bool] = []

        async def _schedule(config, *, refresh=False):
            seen.append(refresh)
            return _KARACHI_SCHEDULE

        _configure(monkeypatch, booking=BookingSettings(provider="calcom", event_type_id=7))
        monkeypatch.setattr(admin_module, "availability_schedule_for", _schedule)
        calcom_client.get("/admin/api/tenants/hotel-mzv/calcom", headers=_bearer())

        assert seen == [True]


def test_unknown_tenant_is_404(calcom_client):
    response = calcom_client.get("/admin/api/tenants/nope/calcom", headers=_bearer())
    assert response.status_code == 404


def test_the_route_requires_a_token(calcom_client):
    assert calcom_client.get("/admin/api/tenants/hotel-mzv/calcom").status_code == 401
