"""Routes in `app/channels/admin.py` for draft/deploy/version history
(Phase 9.1) — `TestClient` with `app/tenancy/admin.py`'s functions
monkeypatched, the same isolation pattern `test_admin_write.py` uses for
`PUT /tenants/{tenant_id}`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.channels.admin as admin_module
from app.channels.admin_auth import AdminPrincipal, require_admin
from app.config import reset_settings_cache
from app.main import app
from app.tenancy.admin import (
    LiveVersionDeleteError,
    NoDraftError,
    VersionNotFoundError,
)

_TOKEN = "s3cret-admin-token-that-is-long-enough-to-pass-preflight"


def _bearer(token: str = _TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _as_tenant_principal(tenant_id: str = "hotel-mzv"):
    app.dependency_overrides[require_admin] = lambda: AdminPrincipal(
        kind="tenant", tenant_ids=(tenant_id,), subject="user_123"
    )


@pytest.fixture(autouse=True)
def _supabase_configured(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-secret-key")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setenv("ADMIN_ENABLED", "true")
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TOKEN)
    reset_settings_cache()

    async def _no_draft(tenant_id, *, client=None):
        return None, None

    async def _no_live_version(tenant_id, *, client=None):
        return None

    async def _version_v2(tenant_id, *, client=None):
        return "v2"

    monkeypatch.setattr(admin_module.tenancy_admin, "get_draft", _no_draft)
    monkeypatch.setattr(admin_module.tenancy_admin, "get_live_version", _no_live_version)
    monkeypatch.setattr(admin_module.tenancy_admin, "get_tenant_version", _version_v2)

    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(require_admin, None)
    reset_settings_cache()


class TestGetTenantRoute:
    def test_no_draft_has_config_equal_live_config(self, admin_client):
        body = admin_client.get("/admin/api/tenants/hotel-mzv", headers=_bearer()).json()
        assert body["has_draft"] is False
        assert body["config"] == body["live_config"]
        assert body["_draft_version"] is None

    def test_a_draft_is_shown_as_config_distinct_from_live_config(self, admin_client, monkeypatch):
        async def _fake_get_draft(tenant_id, *, client=None):
            from app.tenancy.loader import get_tenant_config

            draft = get_tenant_config(tenant_id).model_copy(update={"greeting": "Draft greeting"})
            return draft, "d1"

        monkeypatch.setattr(admin_module.tenancy_admin, "get_draft", _fake_get_draft)

        body = admin_client.get("/admin/api/tenants/hotel-mzv", headers=_bearer()).json()
        assert body["has_draft"] is True
        assert body["config"]["greeting"] == "Draft greeting"
        assert body["live_config"]["greeting"] != "Draft greeting"
        assert body["_draft_version"] == "d1"


class TestDeployRoute:
    def test_deploy_publishes_the_draft(self, admin_client, monkeypatch):
        seen = {}

        async def _fake_deploy(tenant_id, *, note="", deployed_by="", client=None):
            seen["tenant_id"] = tenant_id
            seen["note"] = note
            seen["deployed_by"] = deployed_by

            from app.db.models import TenantVersion
            from app.tenancy.loader import get_tenant_config

            return TenantVersion(
                tenant_id=tenant_id,
                version_number=2,
                config=get_tenant_config(tenant_id).model_dump(mode="json"),
                note=note,
                is_live=True,
            )

        monkeypatch.setattr(admin_module.tenancy_admin, "deploy_tenant", _fake_deploy)

        response = admin_client.post(
            "/admin/api/tenants/hotel-mzv/deploy",
            json={"note": "ship it"},
            headers=_bearer(),
        )
        assert response.status_code == 200
        assert seen["tenant_id"] == "hotel-mzv"
        assert seen["note"] == "ship it"
        assert seen["deployed_by"] == "operator"

    def test_deploy_with_no_body_defaults_to_an_empty_note(self, admin_client, monkeypatch):
        seen = {}

        async def _fake_deploy(tenant_id, *, note="", deployed_by="", client=None):
            seen["note"] = note
            from app.db.models import TenantVersion
            from app.tenancy.loader import get_tenant_config

            return TenantVersion(
                tenant_id=tenant_id,
                version_number=1,
                config=get_tenant_config(tenant_id).model_dump(mode="json"),
                is_live=True,
            )

        monkeypatch.setattr(admin_module.tenancy_admin, "deploy_tenant", _fake_deploy)
        response = admin_client.post("/admin/api/tenants/hotel-mzv/deploy", headers=_bearer())
        assert response.status_code == 200
        assert seen["note"] == ""

    def test_no_draft_maps_to_409(self, admin_client, monkeypatch):
        async def _raise(tenant_id, *, note="", deployed_by="", client=None):
            raise NoDraftError(f"tenant {tenant_id!r} has no draft to deploy")

        monkeypatch.setattr(admin_module.tenancy_admin, "deploy_tenant", _raise)
        response = admin_client.post("/admin/api/tenants/hotel-mzv/deploy", headers=_bearer())
        assert response.status_code == 409

    def test_a_no_longer_valid_draft_maps_to_422(self, admin_client, monkeypatch):
        from pydantic import ValidationError

        async def _raise(tenant_id, *, note="", deployed_by="", client=None):
            try:
                from app.tenancy.models import TenantConfig

                TenantConfig.model_validate({"tenant_id": "x"})
            except ValidationError as exc:
                raise exc

        monkeypatch.setattr(admin_module.tenancy_admin, "deploy_tenant", _raise)
        response = admin_client.post("/admin/api/tenants/hotel-mzv/deploy", headers=_bearer())
        assert response.status_code == 422
        assert response.json()["detail"]

    def test_voice_consent_required_maps_to_409(self, admin_client, monkeypatch):
        from app.tenancy.admin import VoiceConsentRequiredError

        async def _raise(tenant_id, *, note="", deployed_by="", client=None):
            raise VoiceConsentRequiredError("no consent — run onboard_tenant")

        monkeypatch.setattr(admin_module.tenancy_admin, "deploy_tenant", _raise)
        response = admin_client.post("/admin/api/tenants/hotel-mzv/deploy", headers=_bearer())
        assert response.status_code == 409
        assert "onboard_tenant" in response.json()["detail"]

    def test_deploy_is_operator_only(self, admin_client):
        _as_tenant_principal()
        try:
            response = admin_client.post("/admin/api/tenants/hotel-mzv/deploy", headers=_bearer())
        finally:
            app.dependency_overrides.pop(require_admin, None)
        assert response.status_code == 403


class TestDiscardDraftRoute:
    def test_discard_is_not_operator_only(self, admin_client, monkeypatch):
        called = {"n": 0}

        async def _fake_discard(tenant_id, *, client=None):
            called["n"] += 1

        monkeypatch.setattr(admin_module.tenancy_admin, "discard_draft", _fake_discard)

        _as_tenant_principal()
        try:
            response = admin_client.post(
                "/admin/api/tenants/hotel-mzv/draft/discard", headers=_bearer()
            )
        finally:
            app.dependency_overrides.pop(require_admin, None)
        assert response.status_code == 200
        assert called["n"] == 1


class TestListVersionsRoute:
    def test_lists_versions(self, admin_client, monkeypatch):
        from app.db.models import TenantVersion

        async def _fake_list(tenant_id, *, limit=50, client=None):
            from app.tenancy.loader import get_tenant_config

            dumped = get_tenant_config(tenant_id).model_dump(mode="json")
            return [
                TenantVersion(tenant_id=tenant_id, version_number=2, config=dumped, is_live=True),
                TenantVersion(tenant_id=tenant_id, version_number=1, config=dumped, is_live=False),
            ]

        monkeypatch.setattr(admin_module.tenancy_admin, "list_versions", _fake_list)
        response = admin_client.get("/admin/api/tenants/hotel-mzv/versions", headers=_bearer())
        assert response.status_code == 200
        versions = response.json()["versions"]
        assert [v["version_number"] for v in versions] == [2, 1]


class TestSwitchVersionRoute:
    def test_switch_is_operator_only(self, admin_client):
        _as_tenant_principal()
        try:
            response = admin_client.post(
                "/admin/api/tenants/hotel-mzv/versions/v1/switch", headers=_bearer()
            )
        finally:
            app.dependency_overrides.pop(require_admin, None)
        assert response.status_code == 403

    def test_unknown_version_is_404(self, admin_client, monkeypatch):
        async def _raise(tenant_id, version_id, *, client=None):
            raise VersionNotFoundError(f"no version {version_id!r}")

        monkeypatch.setattr(admin_module.tenancy_admin, "switch_to_version", _raise)
        response = admin_client.post(
            "/admin/api/tenants/hotel-mzv/versions/nope/switch", headers=_bearer()
        )
        assert response.status_code == 404

    def test_a_successful_switch_returns_the_updated_tenant(self, admin_client, monkeypatch):
        async def _fake_switch(tenant_id, version_id, *, client=None):
            return None

        monkeypatch.setattr(admin_module.tenancy_admin, "switch_to_version", _fake_switch)
        response = admin_client.post(
            "/admin/api/tenants/hotel-mzv/versions/v1/switch", headers=_bearer()
        )
        assert response.status_code == 200
        assert response.json()["config"]["tenant_id"] == "hotel-mzv"


class TestDeleteVersionRoute:
    def test_delete_is_operator_only(self, admin_client):
        _as_tenant_principal()
        try:
            response = admin_client.post(
                "/admin/api/tenants/hotel-mzv/versions/v1/delete", headers=_bearer()
            )
        finally:
            app.dependency_overrides.pop(require_admin, None)
        assert response.status_code == 403

    def test_live_version_delete_maps_to_409(self, admin_client, monkeypatch):
        async def _raise(tenant_id, version_id, *, client=None):
            raise LiveVersionDeleteError(f"version {version_id!r} is live")

        monkeypatch.setattr(admin_module.tenancy_admin, "delete_version", _raise)
        response = admin_client.post(
            "/admin/api/tenants/hotel-mzv/versions/v1/delete", headers=_bearer()
        )
        assert response.status_code == 409

    def test_a_successful_delete(self, admin_client, monkeypatch):
        called = {"n": 0}

        async def _fake_delete(tenant_id, version_id, *, client=None):
            called["n"] += 1

        monkeypatch.setattr(admin_module.tenancy_admin, "delete_version", _fake_delete)
        response = admin_client.post(
            "/admin/api/tenants/hotel-mzv/versions/v1/delete", headers=_bearer()
        )
        assert response.status_code == 200
        assert response.json() == {"deleted": "v1"}
        assert called["n"] == 1


class TestTestLinkRoute:
    def test_missing_public_base_url_is_422(self, admin_client):
        response = admin_client.post("/admin/api/tenants/hotel-mzv/test-link", headers=_bearer())
        assert response.status_code == 422

    def test_mints_a_link_under_the_configured_base_url(self, admin_client, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
        reset_settings_cache()
        try:
            response = admin_client.post(
                "/admin/api/tenants/hotel-mzv/test-link",
                json={"mode": "chat"},
                headers=_bearer(),
            )
        finally:
            reset_settings_cache()
        assert response.status_code == 200
        body = response.json()
        assert body["url"].startswith("https://example.test/test/")
        assert body["expires_at"] > 0

    def test_unknown_tenant_is_404(self, admin_client, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
        reset_settings_cache()
        try:
            response = admin_client.post(
                "/admin/api/tenants/does-not-exist/test-link", headers=_bearer()
            )
        finally:
            reset_settings_cache()
        assert response.status_code == 404
