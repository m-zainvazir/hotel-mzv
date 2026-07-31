"""Admin dashboard read routes (Phase 8) — exercised against the real
`app.main.app` via `TestClient`, the same pattern `tests/test_api.py` uses.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.channels.admin_auth import AdminPrincipal, require_admin
from app.config import reset_settings_cache
from app.db.memory_store import get_store
from app.db.models import Call, ChatMessage, ChatSession, Escalation, Job
from app.main import app

_TOKEN = "s3cret-admin-token-that-is-long-enough-to-pass-preflight"


def _bearer(token: str = _TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setenv("ADMIN_ENABLED", "true")
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TOKEN)
    reset_settings_cache()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(require_admin, None)
    reset_settings_cache()


def test_admin_disabled_by_default_is_404():
    """No ADMIN_ENABLED set at all -- the default -- 404s, the same as a
    route that was never registered."""
    with TestClient(app) as anon_client:
        response = anon_client.get("/admin/api/session")
    assert response.status_code == 404


def test_admin_enabled_but_no_bearer_is_401(admin_client):
    assert admin_client.get("/admin/api/session").status_code == 401


def test_session_reports_operator_kind(admin_client):
    response = admin_client.get("/admin/api/session", headers=_bearer())
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "operator"
    assert body["tenant_ids"] is None
    assert set(body["capabilities"]) == {"read", "write"}


def test_list_tenants_returns_the_configured_tenants(admin_client):
    response = admin_client.get("/admin/api/tenants", headers=_bearer())
    assert response.status_code == 200
    assert {"hotel-mzv", "northside-plumbing"} <= set(response.json()["tenant_ids"])


def test_get_tenant_returns_config_and_health_flags(admin_client):
    response = admin_client.get("/admin/api/tenants/hotel-mzv", headers=_bearer())
    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "hotel-mzv"
    assert body["greeting"]
    assert "_health" in body
    assert body["_health"]["booking_provider"] in {"stub", "calcom"}


def test_get_unknown_tenant_is_404(admin_client):
    response = admin_client.get("/admin/api/tenants/does-not-exist", headers=_bearer())
    assert response.status_code == 404


def test_get_tenant_includes_the_rendered_system_prompt(admin_client):
    """The admin panel's AI Prompt tab needs a real starting point, not a
    blank box, on a tenant with no override set yet."""
    response = admin_client.get("/admin/api/tenants/hotel-mzv", headers=_bearer())
    body = response.json()
    assert body["system_prompt_override"] is None
    assert "Hotel_MZV" in body["_rendered_system_prompt"]
    assert "## Safety" in body["_rendered_system_prompt"]


def test_calls_list_response_has_no_transcript_key(admin_client):
    get_store().record_call(
        Call(
            tenant_id="hotel-mzv",
            provider_call_id="p1",
            transcript="a private conversation that must not leak into the list",
        )
    )
    response = admin_client.get("/admin/api/tenants/hotel-mzv/calls", headers=_bearer())
    assert response.status_code == 200
    calls = response.json()["calls"]
    assert len(calls) == 1
    assert "transcript" not in calls[0]
    assert "recording_url" not in calls[0]


def test_get_call_returns_the_full_record_including_transcript(admin_client):
    call = get_store().record_call(
        Call(tenant_id="hotel-mzv", provider_call_id="p1", transcript="the full text")
    )
    response = admin_client.get(f"/admin/api/tenants/hotel-mzv/calls/{call.id}", headers=_bearer())
    assert response.status_code == 200
    assert response.json()["transcript"] == "the full text"


def test_get_unknown_call_is_404(admin_client):
    response = admin_client.get(
        "/admin/api/tenants/hotel-mzv/calls/call_doesnotexist", headers=_bearer()
    )
    assert response.status_code == 404


def test_chats_list_and_message_detail(admin_client):
    get_store().start_chat_session(ChatSession(id="web_1", tenant_id="hotel-mzv"))
    get_store().record_chat_message(
        ChatMessage(tenant_id="hotel-mzv", session_id="web_1", role="user", content="hi")
    )

    sessions_response = admin_client.get("/admin/api/tenants/hotel-mzv/chats", headers=_bearer())
    assert sessions_response.status_code == 200
    assert sessions_response.json()["sessions"][0]["id"] == "web_1"

    messages_response = admin_client.get(
        "/admin/api/tenants/hotel-mzv/chats/web_1", headers=_bearer()
    )
    assert messages_response.status_code == 200
    body = messages_response.json()
    assert body["session"]["id"] == "web_1"
    assert body["messages"][0]["content"] == "hi"


def test_get_unknown_chat_session_is_404(admin_client):
    response = admin_client.get(
        "/admin/api/tenants/hotel-mzv/chats/does-not-exist", headers=_bearer()
    )
    assert response.status_code == 404


def test_jobs_list_respects_the_time_window(admin_client):
    get_store().add(
        Job(
            tenant_id="hotel-mzv",
            customer_name="A",
            customer_phone="+1",
            address="",
            service_slug="room-reservation",
            service_name="Room reservation",
            scheduled_start=datetime(2026, 8, 1, 14, tzinfo=UTC),
            scheduled_end=datetime(2026, 8, 1, 15, tzinfo=UTC),
        )
    )
    response = admin_client.get("/admin/api/tenants/hotel-mzv/jobs", headers=_bearer())
    assert response.status_code == 200
    assert len(response.json()["jobs"]) == 1


def test_escalations_list(admin_client):
    get_store().record_escalation(
        Escalation(tenant_id="hotel-mzv", reason="gas leak", transferred_to="+1555")
    )
    response = admin_client.get("/admin/api/tenants/hotel-mzv/escalations", headers=_bearer())
    assert response.status_code == 200
    assert response.json()["escalations"][0]["reason"] == "gas leak"


def test_metrics_route_returns_totals_and_daily_series(admin_client):
    get_store().record_call(Call(tenant_id="hotel-mzv", provider_call_id="p1", duration_seconds=60))
    response = admin_client.get("/admin/api/tenants/hotel-mzv/metrics", headers=_bearer())
    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["calls"] == 1
    assert isinstance(body["daily"], list)


def test_overview_lists_every_accessible_tenant(admin_client):
    response = admin_client.get("/admin/api/overview", headers=_bearer())
    assert response.status_code == 200
    tenant_ids = {row["tenant_id"] for row in response.json()["tenants"]}
    assert {"hotel-mzv", "northside-plumbing"} <= tenant_ids


def test_overview_degrades_one_tenants_failure_without_failing_the_page(admin_client, monkeypatch):
    """A Supabase hiccup on one tenant must not blank the dashboard for
    every tenant — this is what makes the operator landing page a per-tenant
    loop rather than one all-or-nothing query."""
    import app.channels.admin as admin_module

    original = admin_module.get_tenant_config

    def _flaky(tenant_id: str):
        if tenant_id == "northside-plumbing":
            raise RuntimeError("simulated Supabase hiccup")
        return original(tenant_id)

    monkeypatch.setattr(admin_module, "get_tenant_config", _flaky)

    response = admin_client.get("/admin/api/overview", headers=_bearer())
    assert response.status_code == 200
    rows = {row["tenant_id"]: row for row in response.json()["tenants"]}
    assert "error" in rows["northside-plumbing"]
    assert "error" not in rows["hotel-mzv"]


def test_a_tenant_scoped_principal_is_forbidden_from_another_tenant(admin_client):
    """No real tenant-login branch exists yet -- simulate the future path via
    FastAPI's dependency_overrides, exactly the seam plans/phase8.md's
    "designed for tenant login later" contract is built around."""
    app.dependency_overrides[require_admin] = lambda: AdminPrincipal(
        kind="tenant", tenant_ids=("hotel-mzv",), subject="user_123"
    )
    try:
        own = admin_client.get("/admin/api/tenants/hotel-mzv")
        other = admin_client.get("/admin/api/tenants/northside-plumbing")
    finally:
        app.dependency_overrides.pop(require_admin, None)

    assert own.status_code == 200
    assert other.status_code == 403
