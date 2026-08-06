"""Admin write path (Phase 8) — two layers, tested separately.

`app/tenancy/admin.py`'s `save_tenant`/`operator_only_violations` are unit-
tested directly with an injected `httpx.MockTransport` client, the same
pattern `test_tenant_sync.py` uses for `sync_tenant`. The `PUT
/admin/api/tenants/{tenant_id}` route in `app/channels/admin.py` is tested
via `TestClient` with `tenancy_admin.save_tenant`/`get_tenant_version`
monkeypatched — it has no `client=` of its own to inject (by design: it
always talks to the real project), so its own logic (merge, validate,
error-mapping, the operator-only gate) is exercised in isolation from the
Supabase-backed implementation underneath, which is what the direct unit
tests above already cover.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

import app.channels.admin as admin_module
from app.channels.admin_auth import AdminPrincipal, require_admin
from app.config import get_settings, reset_settings_cache
from app.main import app
from app.tenancy import loader
from app.tenancy.admin import (
    VersionConflictError,
    VoiceConsentRequiredError,
    operator_only_violations,
    save_tenant,
)
from tests.conftest import mock_http

_TOKEN = "s3cret-admin-token-that-is-long-enough-to-pass-preflight"


@pytest.fixture(autouse=True)
def _supabase_configured(monkeypatch):
    """`sync_tenant` (and therefore `save_tenant`) checks SUPABASE_URL/
    SUPABASE_SECRET_KEY unconditionally, even with a client override — same
    convention `test_tenant_sync.py` follows."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-secret-key")
    reset_settings_cache()
    yield
    reset_settings_cache()


def _bearer(token: str = _TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _tenants_get(updated_at: str, voice_id: str | None = None) -> httpx.Response:
    return httpx.Response(
        200, json=[{"updated_at": updated_at, "config": {"voice": {"voice_id": voice_id}}}]
    )


# --- app/tenancy/admin.py: direct unit tests --------------------------------


def test_admin_client_carries_the_upsert_headers_sync_tenant_depends_on():
    """Live-found regression: app/tenancy/admin.py used to build its OWN
    Supabase client, missing the `Prefer: resolution=merge-duplicates`
    header sync_tenant()'s upsert semantics depend on. save_tenant() passes
    that client into sync_tenant(config, client=active) — and sync_tenant
    only sets Prefer when it builds its OWN client, which an injected one
    deliberately bypasses (the same convention every provider in this
    codebase follows, so a test can inject bare headers). The result was a
    live 409 duplicate-key error on every admin-panel save: PostgREST fell
    back to a plain INSERT instead of an upsert. No offline test caught this
    because mock_http's mock doesn't simulate PostgREST's Prefer-dependent
    upsert-vs-insert behaviour — only a real request against a real
    database exposes it. Fixed by deleting the duplicate and reusing
    app/tenancy/sync.py's own (already-correct, already-tested-in-
    production-via-scripts/sync_tenants.py) client builder instead of
    re-implementing it a second time."""
    from app.tenancy.admin import _admin_client

    client = _admin_client(get_settings(), timeout=8.0)
    assert "merge-duplicates" in client.headers["prefer"]
    assert "return=representation" in client.headers["prefer"]


class TestSaveTenant:
    async def test_successful_save_reaches_sync_tenant(self, hotel):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/tenants" and request.method == "GET":
                return _tenants_get("v1")
            if request.url.path == "/tenants" and request.method == "POST":
                return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])
            return httpx.Response(200, json=[])

        client, requests = mock_http(handler)
        result = await save_tenant(hotel, expected_version=None, client=client)

        assert result == hotel
        assert any(r.method == "POST" and r.url.path == "/tenants" for r in requests)

    async def test_a_removed_service_produces_a_delete_for_the_orphan(self, hotel):
        """save_tenant calls straight through to sync_tenant, which now
        deletes anything the config no longer declares — see
        test_tenant_sync.py for the exhaustive version of this."""
        fewer_services = hotel.model_copy(update={"services": hotel.services[:1]})

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/tenants" and request.method == "GET":
                return _tenants_get("v1")
            return httpx.Response(200, json=[{"tenant_id": hotel.tenant_id}])

        client, requests = mock_http(handler)
        await save_tenant(fewer_services, expected_version=None, client=client)

        delete = next(r for r in requests if r.url.path == "/services" and r.method == "DELETE")
        assert "slug" in dict(delete.url.params)

    async def test_no_expected_version_skips_the_concurrency_check(self, hotel):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/tenants" and request.method == "GET":
                return _tenants_get("some-other-value-entirely")
            return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])

        client, _ = mock_http(handler)
        result = await save_tenant(hotel, expected_version=None, client=client)
        assert result == hotel

    async def test_matching_version_succeeds(self, hotel):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/tenants" and request.method == "GET":
                return _tenants_get("v1")
            return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])

        client, _ = mock_http(handler)
        result = await save_tenant(hotel, expected_version="v1", client=client)
        assert result == hotel

    async def test_stale_version_raises_version_conflict(self, hotel):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/tenants" and request.method == "GET":
                return _tenants_get("v2-newer-than-what-the-caller-saw")
            return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])

        client, requests = mock_http(handler)
        with pytest.raises(VersionConflictError):
            await save_tenant(hotel, expected_version="v1-stale", client=client)

        # The rejected write must leave nothing behind: no POST ever sent.
        assert not [r for r in requests if r.method == "POST"]

    async def test_setting_a_new_voice_id_without_consent_raises(self, hotel):
        changed = hotel.model_copy(
            update={"voice": hotel.voice.model_copy(update={"voice_id": "brand-new-voice"})}
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/tenants" and request.method == "GET":
                return _tenants_get("v1", voice_id=None)
            if request.url.path == "/voice_consents":
                return httpx.Response(200, json=[])  # no consent row
            return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])

        client, requests = mock_http(handler)
        with pytest.raises(VoiceConsentRequiredError, match="onboard_tenant"):
            await save_tenant(changed, expected_version=None, client=client)

        assert not [r for r in requests if r.method == "POST"]

    async def test_setting_a_new_voice_id_with_consent_succeeds(self, hotel):
        changed = hotel.model_copy(
            update={"voice": hotel.voice.model_copy(update={"voice_id": "brand-new-voice"})}
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/tenants" and request.method == "GET":
                return _tenants_get("v1", voice_id=None)
            if request.url.path == "/voice_consents":
                return httpx.Response(200, json=[{"tenant_id": hotel.tenant_id}])
            return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])

        client, _ = mock_http(handler)
        result = await save_tenant(changed, expected_version=None, client=client)
        assert result.voice.voice_id == "brand-new-voice"

    async def test_unrelated_edit_on_a_tenant_with_an_existing_voice_never_checks_consent(
        self, hotel
    ):
        """The trigger-interaction bug this whole design exists to avoid:
        editing an unrelated field on a tenant that already has a voice_id
        (and, implicitly, already has a consent row from when it was
        cloned) must not require re-checking consent."""
        consent_checked = {"called": False}
        edited = hotel.model_copy(
            update={
                "voice": hotel.voice.model_copy(update={"voice_id": "already-set-voice"}),
                "greeting": "A brand new greeting",
            }
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/tenants" and request.method == "GET":
                return _tenants_get("v1", voice_id="already-set-voice")
            if request.url.path == "/voice_consents":
                consent_checked["called"] = True
                return httpx.Response(200, json=[])
            return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])

        client, _ = mock_http(handler)
        result = await save_tenant(edited, expected_version=None, client=client)

        assert result.greeting == "A brand new greeting"
        assert consent_checked["called"] is False

    async def test_a_p0001_error_from_the_database_maps_to_voice_consent_required(self, hotel):
        """Covers the race the pre-check can't close on its own: a consent
        row removed between the pre-check and the write landing."""
        changed = hotel.model_copy(
            update={"voice": hotel.voice.model_copy(update={"voice_id": "brand-new-voice"})}
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/tenants" and request.method == "GET":
                return _tenants_get("v1", voice_id=None)
            if request.url.path == "/voice_consents":
                # passes the pre-check
                return httpx.Response(200, json=[{"tenant_id": hotel.tenant_id}])
            if request.url.path == "/tenants" and request.method == "POST":
                return httpx.Response(
                    400, text='{"code":"P0001","message":"no voice_consents row"}'
                )
            return httpx.Response(200, json=[])

        client, _ = mock_http(handler)
        with pytest.raises(VoiceConsentRequiredError):
            await save_tenant(changed, expected_version=None, client=client)

    async def test_save_clears_and_refreshes_the_repository(self, hotel):
        class _FakeRepo:
            def __init__(self) -> None:
                self.refreshed = False

            async def refresh(self) -> None:
                self.refreshed = True

            def get(self, tenant_id: str):
                return hotel

            def list_ids(self) -> list[str]:
                return [hotel.tenant_id]

            def find_by_phone(self, phone_number: str):
                return None

            def find_by_widget_key(self, widget_key: str):
                return None

            def find_by_assistant_id(self, assistant_id: str):
                return None

        fake = _FakeRepo()
        loader.set_repository(fake)
        try:

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/tenants" and request.method == "GET":
                    return _tenants_get("v1")
                return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])

            client, _ = mock_http(handler)
            await save_tenant(hotel, expected_version=None, client=client)
            assert fake.refreshed is True
        finally:
            loader.set_repository(None)


class TestOperatorOnlyViolations:
    def test_no_violations_for_an_ordinary_edit(self, hotel):
        edited = hotel.model_copy(update={"greeting": "New greeting"})
        assert operator_only_violations(hotel, edited) == []

    def test_detects_a_changed_top_level_operator_only_field(self, hotel):
        edited = hotel.model_copy(update={"status": "paused"})
        assert "status" in operator_only_violations(hotel, edited)

    def test_detects_a_changed_nested_operator_only_field(self, hotel):
        edited = hotel.model_copy(
            update={"voice": hotel.voice.model_copy(update={"voice_id": "x"})}
        )
        assert "voice.voice_id" in operator_only_violations(hotel, edited)

    def test_a_sibling_field_under_the_same_parent_is_not_flagged(self, hotel):
        """booking.event_type_id is operator-only, but booking itself isn't —
        editing require_address must not falsely flag event_type_id."""
        edited = hotel.model_copy(
            update={
                "booking": hotel.booking.model_copy(
                    update={"require_address": not hotel.booking.require_address}
                )
            }
        )
        assert "booking.event_type_id" not in operator_only_violations(hotel, edited)


# --- app/channels/admin.py's PUT route: isolated from the real Supabase call


@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setenv("ADMIN_ENABLED", "true")
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TOKEN)
    reset_settings_cache()

    # Phase 9.1: `_tenant_detail` (app/channels/admin.py) — used by GET, PUT,
    # and every lifecycle route — now reads the draft + live-version rows on
    # every response. Defaulted here to "no draft, no recorded live version"
    # so a test that doesn't care about drafts/versions doesn't need to know
    # these calls exist, and (more importantly) doesn't trip `no_network`:
    # the real functions build a real httpx client the moment
    # SUPABASE_URL/SUPABASE_SECRET_KEY are set, which `_supabase_configured`
    # always does in this file.
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


class TestPutTenantRoute:
    """Phase 9.1: PUT writes the DRAFT, never live — `save_draft`, not
    `save_tenant`. The response nests the effective (draft-or-live) config
    under `config`, and the always-live one under `live_config`."""

    def test_a_valid_edit_is_saved_as_a_draft_not_live(self, admin_client, monkeypatch):
        saved: dict = {}

        async def _fake_save_draft(config, *, expected_version, client=None):
            saved["config"] = config
            saved["expected_version"] = expected_version
            return "draft-v1"

        async def _fake_get_draft(tenant_id, *, client=None):
            return (saved.get("config"), "draft-v1" if "config" in saved else None)

        monkeypatch.setattr(admin_module.tenancy_admin, "save_draft", _fake_save_draft)
        monkeypatch.setattr(admin_module.tenancy_admin, "get_draft", _fake_get_draft)

        response = admin_client.put(
            "/admin/api/tenants/hotel-mzv",
            json={"greeting": "New greeting text"},
            headers=_bearer(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["config"]["greeting"] == "New greeting text"
        assert body["live_config"]["greeting"] != "New greeting text"
        assert body["has_draft"] is True
        assert body["_draft_version"] == "draft-v1"
        assert saved["config"].greeting == "New greeting text"
        assert saved["expected_version"] is None

    def test_a_system_prompt_override_is_saved_and_reflected_in_the_render(
        self, admin_client, monkeypatch
    ):
        """Not an operator-only field (unlike voice_id/mcp_servers) — behaviour
        text, same category as greeting/persona."""
        saved: dict = {}

        async def _fake_save_draft(config, *, expected_version, client=None):
            saved["config"] = config
            return "draft-v1"

        async def _fake_get_draft(tenant_id, *, client=None):
            return (saved.get("config"), "draft-v1" if "config" in saved else None)

        monkeypatch.setattr(admin_module.tenancy_admin, "save_draft", _fake_save_draft)
        monkeypatch.setattr(admin_module.tenancy_admin, "get_draft", _fake_get_draft)

        response = admin_client.put(
            "/admin/api/tenants/hotel-mzv",
            json={"system_prompt_override": "You are ${business_name}'s custom receptionist."},
            headers=_bearer(),
        )
        assert response.status_code == 200
        body = response.json()
        assert (
            body["config"]["system_prompt_override"]
            == "You are ${business_name}'s custom receptionist."
        )
        assert "custom receptionist" in body["_rendered_system_prompt"]

    def test_clearing_a_system_prompt_override_falls_back_to_the_shared_default(
        self, admin_client, monkeypatch
    ):
        saved: dict = {}

        async def _fake_save_draft(config, *, expected_version, client=None):
            saved["config"] = config
            return "draft-v1"

        async def _fake_get_draft(tenant_id, *, client=None):
            return (saved.get("config"), "draft-v1" if "config" in saved else None)

        monkeypatch.setattr(admin_module.tenancy_admin, "save_draft", _fake_save_draft)
        monkeypatch.setattr(admin_module.tenancy_admin, "get_draft", _fake_get_draft)

        response = admin_client.put(
            "/admin/api/tenants/hotel-mzv",
            json={"system_prompt_override": None},
            headers=_bearer(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["config"]["system_prompt_override"] is None
        assert "## Safety" in body["_rendered_system_prompt"]

    @pytest.mark.parametrize(
        "payload,expected_loc",
        [
            ({"timezone": "Mars/Phobos"}, ["timezone"]),
            ({"hours": {"someday": None}}, ["hours"]),
            (
                {"services": [{"slug": "x", "name": "X"}, {"slug": "x", "name": "X2"}]},
                [],  # a model-level validator; no single field path
            ),
            ({"booking": {"provider": "calcom"}}, []),
            ({"hours": {"monday": {"open": "10:00", "close": "09:00"}}}, ["hours", "monday"]),
            (
                {"mcp_servers": [{"name": "My Server", "transport": "http", "url": "http://x"}]},
                ["mcp_servers", 0, "name"],
            ),
            ({"voice": {"speed": 3.0}}, ["voice", "speed"]),
            (
                {"services": [{"slug": "x", "name": "X", "duration_minutes": 600}]},
                ["services", 0, "duration_minutes"],
            ),
        ],
    )
    def test_every_existing_validator_produces_a_422_with_a_field_path(
        self, admin_client, payload, expected_loc
    ):
        response = admin_client.put("/admin/api/tenants/hotel-mzv", json=payload, headers=_bearer())
        assert response.status_code == 422
        errors = response.json()["detail"]
        assert errors  # at least one error was reported
        if expected_loc:
            assert any(list(err["loc"]) == expected_loc for err in errors), errors

    def test_unknown_tenant_is_404(self, admin_client):
        response = admin_client.put("/admin/api/tenants/does-not-exist", json={}, headers=_bearer())
        assert response.status_code == 404

    def test_stale_if_match_against_the_draft_maps_to_409(self, admin_client, monkeypatch):
        async def _raising_save_draft(config, *, expected_version, client=None):
            raise VersionConflictError("someone else saved first")

        monkeypatch.setattr(admin_module.tenancy_admin, "save_draft", _raising_save_draft)

        response = admin_client.put(
            "/admin/api/tenants/hotel-mzv",
            json={"greeting": "x"},
            headers={**_bearer(), "If-Match": "stale-draft-version"},
        )
        assert response.status_code == 409
        assert "someone else" in response.json()["detail"]

    def test_voice_consent_is_not_checked_at_draft_save_time(self, admin_client, monkeypatch):
        """The consent gate moved to deploy/switch — a draft may stage an
        unconsented voice_id change freely; it just can't go live yet."""
        saved: dict = {}

        async def _fake_save_draft(config, *, expected_version, client=None):
            saved["config"] = config
            return "draft-v1"

        monkeypatch.setattr(admin_module.tenancy_admin, "save_draft", _fake_save_draft)

        response = admin_client.put(
            "/admin/api/tenants/hotel-mzv",
            json={"voice": {"voice_id": "new-voice"}},
            headers=_bearer(),
        )
        assert response.status_code == 200
        assert saved["config"].voice.voice_id == "new-voice"

    def test_an_operator_only_field_is_blocked_for_a_tenant_principal(self, admin_client):
        app.dependency_overrides[require_admin] = lambda: AdminPrincipal(
            kind="tenant", tenant_ids=("hotel-mzv",), subject="user_123"
        )
        try:
            response = admin_client.put(
                "/admin/api/tenants/hotel-mzv",
                json={"status": "paused"},
                headers=_bearer(),
            )
        finally:
            app.dependency_overrides.pop(require_admin, None)
        assert response.status_code == 403

    def test_a_tenant_principal_may_still_edit_ordinary_fields(self, admin_client, monkeypatch):
        saved: dict = {}

        async def _fake_save_draft(config, *, expected_version, client=None):
            saved["config"] = config
            return "draft-v1"

        async def _fake_get_draft(tenant_id, *, client=None):
            return (saved.get("config"), "draft-v1" if "config" in saved else None)

        monkeypatch.setattr(admin_module.tenancy_admin, "save_draft", _fake_save_draft)
        monkeypatch.setattr(admin_module.tenancy_admin, "get_draft", _fake_get_draft)

        app.dependency_overrides[require_admin] = lambda: AdminPrincipal(
            kind="tenant", tenant_ids=("hotel-mzv",), subject="user_123"
        )
        try:
            response = admin_client.put(
                "/admin/api/tenants/hotel-mzv",
                json={"greeting": "A tenant-edited greeting"},
                headers=_bearer(),
            )
        finally:
            app.dependency_overrides.pop(require_admin, None)
        assert response.status_code == 200
        assert response.json()["config"]["greeting"] == "A tenant-edited greeting"

    def test_a_tenant_principal_repeating_a_stale_violation_across_saves_is_still_blocked(
        self, admin_client, monkeypatch
    ):
        """Regression guard for comparing against LIVE, not the draft: if the
        diff were computed against the current draft instead, a violation
        already staged in a prior save would stop tripping on every save
        after the first, since it would equal itself."""
        already_violating = None

        async def _fake_get_draft(tenant_id, *, client=None):
            nonlocal already_violating
            if already_violating is None:
                return None, None
            return already_violating, "draft-v1"

        monkeypatch.setattr(admin_module.tenancy_admin, "get_draft", _fake_get_draft)

        app.dependency_overrides[require_admin] = lambda: AdminPrincipal(
            kind="tenant", tenant_ids=("hotel-mzv",), subject="user_123"
        )
        try:
            response = admin_client.put(
                "/admin/api/tenants/hotel-mzv",
                json={"status": "paused"},
                headers=_bearer(),
            )
        finally:
            app.dependency_overrides.pop(require_admin, None)
        assert response.status_code == 403

    def test_body_tenant_id_is_ignored_in_favour_of_the_path(self, admin_client, monkeypatch):
        seen = {}

        async def _fake_save_draft(config, *, expected_version, client=None):
            seen["tenant_id"] = config.tenant_id
            return "draft-v1"

        monkeypatch.setattr(admin_module.tenancy_admin, "save_draft", _fake_save_draft)

        admin_client.put(
            "/admin/api/tenants/hotel-mzv",
            json={"tenant_id": "northside-plumbing", "greeting": "x"},
            headers=_bearer(),
        )
        assert seen["tenant_id"] == "hotel-mzv"
