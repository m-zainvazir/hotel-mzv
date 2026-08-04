"""Bot lifecycle: create / archive / restore / purge (Phase 9 Part B).

Same two-layer split `test_admin_write.py` uses: `app/tenancy/admin.py`'s
`create_tenant` / `set_tenant_status` / `purge_tenant` are unit-tested
directly with an injected `httpx.MockTransport` client; the routes in
`app/channels/admin.py` are tested via `TestClient` with those functions
monkeypatched, isolating request validation / template loading / clone-base
building / auth gates from the Supabase-backed implementation underneath.
The archived-tenant refusal itself (Step B1) is tested against both real
channels — the actual proof the plan asks for, not just a unit check.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.channels.admin as admin_module
from app.channels.admin_auth import AdminPrincipal, require_admin
from app.config import reset_settings_cache
from app.main import app
from app.tenancy import loader
from app.tenancy.admin import (
    TenantAlreadyExistsError,
    TenantNotArchivedError,
    create_tenant,
    purge_tenant,
    set_tenant_status,
)
from app.tenancy.models import TenantConfig
from app.tenancy.repository import TenantArchivedError, TenantNotFoundError
from tests.conftest import mock_http

_TOKEN = "s3cret-admin-token-that-is-long-enough-to-pass-preflight"
FIXTURE = Path(__file__).parent / "fixtures" / "vapi_chat_completion_request.json"


def _bearer(token: str = _TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _tenants_get(*rows: dict) -> httpx.Response:
    return httpx.Response(200, json=list(rows))


class _FakeRepo:
    """Mirrors test_admin_write.py's spy repo — proves the invalidation
    dance ran after a write, without touching a real repository."""

    def __init__(self, tenant: TenantConfig) -> None:
        self.tenant = tenant
        self.refreshed = False

    async def refresh(self) -> None:
        self.refreshed = True

    def get(self, tenant_id: str) -> TenantConfig:
        return self.tenant

    def list_ids(self) -> list[str]:
        return [self.tenant.tenant_id]

    def find_by_phone(self, phone_number: str):
        return self.tenant if phone_number in self.tenant.phone_numbers else None

    def find_by_widget_key(self, widget_key: str):
        return self.tenant if widget_key in self.tenant.widget_keys else None

    def find_by_assistant_id(self, assistant_id: str):
        return None


class _StaticRepo:
    """A repository serving exactly one tenant on every lookup path —
    channel-resolution tests need `find_by_phone`/`find_by_widget_key` to
    actually see the tenant under test, unlike `override_tenant`
    (tests/conftest.py), whose find_by_* methods delegate to the base JSON
    repository and would silently ignore the override."""

    def __init__(self, tenant: TenantConfig) -> None:
        self.tenant = tenant

    def get(self, tenant_id: str) -> TenantConfig:
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


@pytest.fixture(autouse=True)
def _supabase_configured(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-secret-key")
    reset_settings_cache()
    yield
    reset_settings_cache()


# --- app/tenancy/models.py: tenant_id validator + archived status ----------


class TestTenantIdValidator:
    @pytest.mark.parametrize("legal", ["a1", "hotel-mzv", "a-legal-id", "a" * 48])
    def test_legal_ids_pass(self, hotel, legal):
        assert (
            TenantConfig.model_validate(
                {**hotel.model_dump(mode="json"), "tenant_id": legal}
            ).tenant_id
            == legal
        )

    @pytest.mark.parametrize(
        "illegal",
        ["a", "Hotel-MZV", "hotel_mzv!", "-leading-hyphen", "a" * 49, "", "hotel mzv"],
    )
    def test_illegal_ids_422_at_construction(self, hotel, illegal):
        with pytest.raises(ValueError):
            TenantConfig.model_validate({**hotel.model_dump(mode="json"), "tenant_id": illegal})


class TestArchivedRefusal:
    def test_resolve_tenant_id_refuses_an_archived_explicit_id(self, hotel):
        archived = hotel.model_copy(update={"status": "archived"})
        loader.set_repository(_StaticRepo(archived))
        try:
            with pytest.raises(TenantArchivedError):
                loader.resolve_tenant_id(tenant_id=hotel.tenant_id)
        finally:
            loader.set_repository(None)

    def test_archived_tenant_refuses_the_widget_handshake(self, hotel):
        """Real channel test, not a unit check — POST /chat/session must
        404 for an archived tenant's widget key."""
        archived = hotel.model_copy(update={"status": "archived"})
        loader.set_repository(_StaticRepo(archived))
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/chat/session", json={"widget_key": archived.widget_keys[0]}
                )
        finally:
            loader.set_repository(None)
        assert response.status_code == 404

    def test_archived_tenant_refuses_a_vapi_turn(self, hotel):
        """Real channel test — POST /chat/completions must 404 for an
        archived tenant reached via its dialled number (the fixture's
        assistantId doesn't match any real tenant, so this exercises the
        phone-number fallback path, same as a live call would)."""
        archived = hotel.model_copy(update={"status": "archived"})
        loader.set_repository(_StaticRepo(archived))
        try:
            payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
            payload.pop("_comment", None)
            with TestClient(app) as client:
                response = client.post("/chat/completions", json=payload)
        finally:
            loader.set_repository(None)
        assert response.status_code == 404


# --- app/tenancy/admin.py: create_tenant / set_tenant_status / purge_tenant


class TestCreateTenantUnit:
    async def test_refuses_an_existing_tenant_id(self, hotel):
        new = hotel.model_copy(update={"tenant_id": "brand-new-hotel"})

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/tenants" and request.method == "GET":
                return _tenants_get({"updated_at": "v1", "config": {}})
            raise AssertionError("must not write once an existing row was found")

        client, requests = mock_http(handler)
        with pytest.raises(TenantAlreadyExistsError):
            await create_tenant(new, client=client)
        assert not [r for r in requests if r.method == "POST"]

    async def test_writes_onboarding_then_the_final_status(self, hotel):
        new = hotel.model_copy(update={"tenant_id": "brand-new-hotel", "status": "active"})
        posted_statuses: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/tenants" and request.method == "GET":
                return _tenants_get()  # doesn't exist yet
            if request.url.path == "/tenants" and request.method == "POST":
                posted_statuses.append(json.loads(request.content)["status"])
                return httpx.Response(201, json=[{"tenant_id": new.tenant_id}])
            return httpx.Response(200, json=[])

        client, _requests = mock_http(handler)
        result = await create_tenant(new, client=client)

        assert posted_statuses == ["onboarding", "active"]
        assert result.status == "active"

    async def test_onboarding_status_skips_the_second_write(self, hotel):
        new = hotel.model_copy(update={"tenant_id": "brand-new-hotel", "status": "onboarding"})
        tenant_posts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal tenant_posts
            if request.url.path == "/tenants" and request.method == "GET":
                return _tenants_get()
            if request.url.path == "/tenants" and request.method == "POST":
                tenant_posts += 1
                return httpx.Response(201, json=[{"tenant_id": new.tenant_id}])
            return httpx.Response(200, json=[])

        client, _requests = mock_http(handler)
        await create_tenant(new, client=client)
        assert tenant_posts == 1

    async def test_invalidates_and_refreshes_the_repository(self, hotel):
        new = hotel.model_copy(update={"tenant_id": "brand-new-hotel"})
        fake = _FakeRepo(new)
        loader.set_repository(fake)
        try:

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/tenants" and request.method == "GET":
                    return _tenants_get()
                return httpx.Response(201, json=[{"tenant_id": new.tenant_id}])

            client, _requests = mock_http(handler)
            await create_tenant(new, client=client)
            assert fake.refreshed is True
        finally:
            loader.set_repository(None)


class TestSetTenantStatusUnit:
    async def test_archives_a_tenant(self, hotel):
        loader.set_repository(_StaticRepo(hotel))
        try:
            posted = {}

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/tenants" and request.method == "POST":
                    posted["status"] = json.loads(request.content)["status"]
                    return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])
                return httpx.Response(200, json=[])

            client, _requests = mock_http(handler)
            result = await set_tenant_status(hotel.tenant_id, "archived", client=client)
            assert result.status == "archived"
            assert posted["status"] == "archived"
        finally:
            loader.set_repository(None)

    async def test_unknown_tenant_raises_not_found(self):
        # No repository override — the default JsonFileTenantRepository
        # genuinely has no file for this id.
        with pytest.raises(TenantNotFoundError):
            await set_tenant_status("does-not-exist-anywhere", "archived")


class TestPurgeTenantUnit:
    async def test_refuses_unless_archived(self, hotel):
        assert hotel.status == "active"
        loader.set_repository(_StaticRepo(hotel))
        try:
            with pytest.raises(TenantNotArchivedError):
                await purge_tenant(hotel.tenant_id)
        finally:
            loader.set_repository(None)

    async def test_deletes_in_fk_order(self, hotel, monkeypatch):
        # A tenant_id that matches no real content/tenants/*.json file —
        # purge_tenant's real json_path.unlink() step must never be able to
        # touch a real file from a test (this bit a real file once already).
        archived = hotel.model_copy(update={"tenant_id": "purge-test-tenant", "status": "archived"})
        loader.set_repository(_StaticRepo(archived))
        # Best-effort cleanups aren't under test here.
        monkeypatch.setattr("app.tenancy.admin.delete_tenant_secrets", _noop_async)
        try:
            deleted_tables: list[str] = []

            def handler(request: httpx.Request) -> httpx.Response:
                if request.method == "DELETE":
                    deleted_tables.append(request.url.path.lstrip("/"))
                    return httpx.Response(200, json=[{"id": "row1"}])
                return httpx.Response(200, json=[])

            client, _requests = mock_http(handler)
            counts = await purge_tenant(archived.tenant_id, client=client)
        finally:
            loader.set_repository(None)

        assert deleted_tables == [
            "knowledge_chunks",
            "knowledge_documents",
            "chat_messages",
            "chat_sessions",
            "escalations",
            "messages",
            "jobs",
            "calls",
            "services",
            "mcp_servers",
            "voice_consents",
            "tenants",
        ]
        assert all(count == 1 for count in counts.values())

    async def test_returns_per_table_row_counts(self, hotel, monkeypatch):
        archived = hotel.model_copy(update={"tenant_id": "purge-test-tenant", "status": "archived"})
        loader.set_repository(_StaticRepo(archived))
        monkeypatch.setattr("app.tenancy.admin.delete_tenant_secrets", _noop_async)
        try:

            def handler(request: httpx.Request) -> httpx.Response:
                if request.method == "DELETE" and request.url.path == "/jobs":
                    return httpx.Response(200, json=[{"id": "j1"}, {"id": "j2"}, {"id": "j3"}])
                if request.method == "DELETE":
                    return httpx.Response(200, json=[])
                return httpx.Response(200, json=[])

            client, _requests = mock_http(handler)
            counts = await purge_tenant(archived.tenant_id, client=client)
        finally:
            loader.set_repository(None)

        assert counts["jobs"] == 3
        assert counts["tenants"] == 0

    async def test_a_failed_table_delete_raises_and_stops(self, hotel, monkeypatch):
        archived = hotel.model_copy(update={"tenant_id": "purge-test-tenant", "status": "archived"})
        loader.set_repository(_StaticRepo(archived))
        monkeypatch.setattr("app.tenancy.admin.delete_tenant_secrets", _noop_async)
        try:
            seen_deletes: list[str] = []

            def handler(request: httpx.Request) -> httpx.Response:
                if request.method == "DELETE":
                    seen_deletes.append(request.url.path)
                    if request.url.path == "/escalations":
                        return httpx.Response(500, text="boom")
                    return httpx.Response(200, json=[])
                return httpx.Response(200, json=[])

            client, _requests = mock_http(handler)
            from app.tenancy.admin import TenantPurgeError

            with pytest.raises(TenantPurgeError):
                await purge_tenant(archived.tenant_id, client=client)
        finally:
            loader.set_repository(None)
        # Stopped at the failure — never reached the tables after escalations.
        assert seen_deletes == [
            "/knowledge_chunks",
            "/knowledge_documents",
            "/chat_messages",
            "/chat_sessions",
            "/escalations",
        ]

    async def test_vault_secret_failure_does_not_abort_the_purge(self, hotel, monkeypatch):
        archived = hotel.model_copy(update={"tenant_id": "purge-test-tenant", "status": "archived"})
        loader.set_repository(_StaticRepo(archived))

        from app.tenancy.secrets import TenantSecretError

        async def _raise(*_args, **_kwargs):
            raise TenantSecretError("vault unreachable")

        monkeypatch.setattr("app.tenancy.admin.delete_tenant_secrets", _raise)
        try:
            client, _requests = mock_http(lambda req: httpx.Response(200, json=[]))
            counts = await purge_tenant(archived.tenant_id, client=client)
        finally:
            loader.set_repository(None)
        assert counts["tenants"] == 0  # completed despite the vault failure

    async def test_invalidates_and_refreshes_the_repository(self, hotel, monkeypatch):
        archived = hotel.model_copy(update={"tenant_id": "purge-test-tenant", "status": "archived"})
        monkeypatch.setattr("app.tenancy.admin.delete_tenant_secrets", _noop_async)
        fake = _FakeRepo(archived)
        loader.set_repository(fake)
        try:
            client, _requests = mock_http(lambda req: httpx.Response(200, json=[]))
            await purge_tenant(archived.tenant_id, client=client)
            assert fake.refreshed is True
        finally:
            loader.set_repository(None)


async def _noop_async(*_args, **_kwargs) -> None:
    return None


# --- app/channels/admin.py routes: isolated from the real Supabase call ----


@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setenv("ADMIN_ENABLED", "true")
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TOKEN)
    reset_settings_cache()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(require_admin, None)
    reset_settings_cache()


def _as_tenant_principal(tenant_id: str = "hotel-mzv"):
    app.dependency_overrides[require_admin] = lambda: AdminPrincipal(
        kind="tenant", tenant_ids=(tenant_id,), subject="user_123"
    )


class TestCreateTenantRoute:
    def test_blank_mode_creates_and_returns_the_tenant(self, admin_client, monkeypatch):
        async def _fake_create(config, *, client=None):
            return config

        async def _fake_version(tenant_id, *, client=None):
            return "v1"

        monkeypatch.setattr(admin_module.tenancy_admin, "create_tenant", _fake_create)
        monkeypatch.setattr(admin_module.tenancy_admin, "get_tenant_version", _fake_version)

        response = admin_client.post(
            "/admin/api/tenants",
            json={
                "mode": "blank",
                "tenant_id": "a-brand-new-bot",
                "name": "A Brand New Bot",
                "trade": "hotel",
                "greeting": "Hi there",
                "escalation_phone": "+15550001111",
            },
            headers=_bearer(),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["tenant_id"] == "a-brand-new-bot"
        assert body["status"] == "active"
        assert len(body["widget_keys"]) == 1
        assert body["emergency"]["escalation_phone"] == "+15550001111"

    def test_illegal_tenant_id_slug_422s(self, admin_client):
        response = admin_client.post(
            "/admin/api/tenants",
            json={
                "mode": "blank",
                "tenant_id": "Not A Valid Id!",
                "name": "X",
                "trade": "hotel",
                "greeting": "Hi",
                "escalation_phone": "+15550001111",
            },
            headers=_bearer(),
        )
        assert response.status_code == 422
        errors = response.json()["detail"]
        assert any(list(err["loc"]) == ["tenant_id"] for err in errors), errors

    def test_duplicate_tenant_id_maps_to_409(self, admin_client, monkeypatch):
        async def _raise_duplicate(config, *, client=None):
            raise TenantAlreadyExistsError(f"tenant {config.tenant_id!r} already exists")

        monkeypatch.setattr(admin_module.tenancy_admin, "create_tenant", _raise_duplicate)

        response = admin_client.post(
            "/admin/api/tenants",
            json={
                "mode": "blank",
                "tenant_id": "hotel-mzv",
                "name": "X",
                "trade": "hotel",
                "greeting": "Hi",
                "escalation_phone": "+15550001111",
            },
            headers=_bearer(),
        )
        assert response.status_code == 409

    def test_template_mode_produces_a_valid_config_with_template_services(
        self, admin_client, monkeypatch
    ):
        async def _fake_create(config, *, client=None):
            return config

        async def _fake_version(tenant_id, *, client=None):
            return "v1"

        monkeypatch.setattr(admin_module.tenancy_admin, "create_tenant", _fake_create)
        monkeypatch.setattr(admin_module.tenancy_admin, "get_tenant_version", _fake_version)

        response = admin_client.post(
            "/admin/api/tenants",
            json={
                "mode": "template",
                "template": "clinic",
                "tenant_id": "a-new-clinic",
                "name": "A New Clinic",
                "trade": "clinic",
                "greeting": "Hi, thanks for calling",
                "escalation_phone": "+15550001111",
            },
            headers=_bearer(),
        )
        assert response.status_code == 201
        body = response.json()
        slugs = {s["slug"] for s in body["services"]}
        assert "checkup" in slugs
        # The template's own danger keywords survive; only the phone number
        # was overridden.
        assert "chest pain" in body["emergency"]["keywords"]
        assert body["emergency"]["escalation_phone"] == "+15550001111"

    def test_unknown_template_422s(self, admin_client):
        response = admin_client.post(
            "/admin/api/tenants",
            json={
                "mode": "template",
                "template": "spaceship",
                "tenant_id": "x",
                "name": "X",
                "trade": "x",
                "greeting": "Hi",
                "escalation_phone": "+15550001111",
            },
            headers=_bearer(),
        )
        assert response.status_code == 422

    def test_clone_mode_clears_every_identity_field(self, admin_client, monkeypatch):
        captured = {}

        async def _fake_create(config, *, client=None):
            captured["config"] = config
            return config

        async def _fake_version(tenant_id, *, client=None):
            return "v1"

        monkeypatch.setattr(admin_module.tenancy_admin, "create_tenant", _fake_create)
        monkeypatch.setattr(admin_module.tenancy_admin, "get_tenant_version", _fake_version)

        # northside-plumbing, not hotel-mzv: hotel-mzv is a real "calcom"
        # tenant, and clearing booking.event_type_id on a calcom tenant with
        # no per-service override correctly 422s
        # (TenantConfig._calcom_tenants_declare_event_types) — that's
        # exercised on its own below, not conflated with this test's actual
        # subject (which fields clone clears).
        response = admin_client.post(
            "/admin/api/tenants",
            json={
                "mode": "clone",
                "source_tenant_id": "northside-plumbing",
                "tenant_id": "northside-clone",
                "name": "Northside Clone",
                "trade": "plumber",
                "greeting": "Hi",
                "escalation_phone": "+15550001111",
            },
            headers=_bearer(),
        )
        assert response.status_code == 201
        cloned = captured["config"]
        assert cloned.tenant_id == "northside-clone"
        assert cloned.phone_numbers == []
        assert cloned.vapi.assistant_id is None
        assert cloned.voice.voice_id is None
        assert cloned.booking.event_type_id is None
        # A fresh widget key was generated, not copied from the source.
        assert cloned.widget_keys != ["pk_widget_northside_demo"]
        assert len(cloned.widget_keys) == 1
        # Non-identity content (services) carried over from the source.
        assert {s.slug for s in cloned.services} == {
            "drain-clearing",
            "water-heater-service",
            "leak-repair",
        }

    def test_cloning_a_calcom_tenant_with_no_per_service_override_422s(self, admin_client):
        """A real edge case, not a made-up one: clone clears
        `booking.event_type_id` (Step B4's exact field list) but leaves
        `booking.provider` alone, so cloning a calcom tenant whose services
        have no event_type_id override of their own correctly fails
        Pydantic's own `_calcom_tenants_declare_event_types` validator — the
        operator needs to set a new event type before this tenant can go
        live, exactly as it would for a hand-authored calcom tenant."""
        response = admin_client.post(
            "/admin/api/tenants",
            json={
                "mode": "clone",
                "source_tenant_id": "hotel-mzv",
                "tenant_id": "hotel-mzv-clone",
                "name": "Hotel MZV Clone",
                "trade": "hotel",
                "greeting": "Hi",
                "escalation_phone": "+15550001111",
            },
            headers=_bearer(),
        )
        assert response.status_code == 422

    def test_clone_unknown_source_422s(self, admin_client):
        response = admin_client.post(
            "/admin/api/tenants",
            json={
                "mode": "clone",
                "source_tenant_id": "does-not-exist",
                "tenant_id": "x",
                "name": "X",
                "trade": "x",
                "greeting": "Hi",
                "escalation_phone": "+15550001111",
            },
            headers=_bearer(),
        )
        assert response.status_code == 422

    def test_non_operator_principal_is_403d(self, admin_client):
        _as_tenant_principal()
        try:
            response = admin_client.post(
                "/admin/api/tenants",
                json={
                    "mode": "blank",
                    "tenant_id": "x",
                    "name": "X",
                    "trade": "x",
                    "greeting": "Hi",
                    "escalation_phone": "+15550001111",
                },
                headers=_bearer(),
            )
        finally:
            app.dependency_overrides.pop(require_admin, None)
        assert response.status_code == 403


class TestArchiveRestoreRoute:
    def test_archive_route_calls_set_tenant_status_with_archived(self, admin_client, monkeypatch):
        seen = {}

        async def _fake_status(tenant_id, status, *, client=None):
            seen["tenant_id"], seen["status"] = tenant_id, status
            from app.tenancy.loader import get_tenant_config

            return get_tenant_config("hotel-mzv").model_copy(update={"status": status})

        async def _fake_version(tenant_id, *, client=None):
            return "v1"

        monkeypatch.setattr(admin_module.tenancy_admin, "set_tenant_status", _fake_status)
        monkeypatch.setattr(admin_module.tenancy_admin, "get_tenant_version", _fake_version)

        response = admin_client.post("/admin/api/tenants/hotel-mzv/archive", headers=_bearer())
        assert response.status_code == 200
        assert seen == {"tenant_id": "hotel-mzv", "status": "archived"}
        assert response.json()["status"] == "archived"

    def test_restore_route_calls_set_tenant_status_with_active(self, admin_client, monkeypatch):
        seen = {}

        async def _fake_status(tenant_id, status, *, client=None):
            seen["status"] = status
            from app.tenancy.loader import get_tenant_config

            return get_tenant_config("hotel-mzv").model_copy(update={"status": status})

        async def _fake_version(tenant_id, *, client=None):
            return "v1"

        monkeypatch.setattr(admin_module.tenancy_admin, "set_tenant_status", _fake_status)
        monkeypatch.setattr(admin_module.tenancy_admin, "get_tenant_version", _fake_version)

        response = admin_client.post("/admin/api/tenants/hotel-mzv/restore", headers=_bearer())
        assert response.status_code == 200
        assert seen["status"] == "active"

    def test_archive_unknown_tenant_404s(self, admin_client, monkeypatch):
        async def _raise_not_found(tenant_id, status, *, client=None):
            raise TenantNotFoundError(tenant_id)

        monkeypatch.setattr(admin_module.tenancy_admin, "set_tenant_status", _raise_not_found)

        response = admin_client.post("/admin/api/tenants/does-not-exist/archive", headers=_bearer())
        assert response.status_code == 404


class TestPurgeRoute:
    def test_confirmation_mismatch_422s_and_never_calls_purge(self, admin_client, monkeypatch):
        called = {"n": 0}

        async def _fake_purge(tenant_id, *, client=None):
            called["n"] += 1
            return {}

        monkeypatch.setattr(admin_module.tenancy_admin, "purge_tenant", _fake_purge)

        response = admin_client.post(
            "/admin/api/tenants/hotel-mzv/purge",
            json={"tenant_id": "not-hotel-mzv"},
            headers=_bearer(),
        )
        assert response.status_code == 422
        assert called["n"] == 0

    def test_not_archived_maps_to_409(self, admin_client, monkeypatch):
        async def _raise_not_archived(tenant_id, *, client=None):
            raise TenantNotArchivedError(f"tenant {tenant_id!r} must be archived first")

        monkeypatch.setattr(admin_module.tenancy_admin, "purge_tenant", _raise_not_archived)

        response = admin_client.post(
            "/admin/api/tenants/hotel-mzv/purge",
            json={"tenant_id": "hotel-mzv"},
            headers=_bearer(),
        )
        assert response.status_code == 409

    def test_successful_purge_returns_counts(self, admin_client, monkeypatch):
        async def _fake_purge(tenant_id, *, client=None):
            return {"jobs": 2, "tenants": 1}

        monkeypatch.setattr(admin_module.tenancy_admin, "purge_tenant", _fake_purge)

        response = admin_client.post(
            "/admin/api/tenants/hotel-mzv/purge",
            json={"tenant_id": "hotel-mzv"},
            headers=_bearer(),
        )
        assert response.status_code == 200
        assert response.json() == {"tenant_id": "hotel-mzv", "deleted": {"jobs": 2, "tenants": 1}}

    def test_non_operator_principal_is_403d(self, admin_client):
        _as_tenant_principal()
        try:
            response = admin_client.post(
                "/admin/api/tenants/hotel-mzv/purge",
                json={"tenant_id": "hotel-mzv"},
                headers=_bearer(),
            )
        finally:
            app.dependency_overrides.pop(require_admin, None)
        assert response.status_code == 403
