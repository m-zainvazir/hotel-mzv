"""app/tenancy/admin.py's draft/deploy/version-history functions (Phase 9.1)
— unit-tested directly with an injected `httpx.MockTransport` client, the
same pattern `test_admin_write.py`'s `TestSaveTenant` uses for `save_tenant`.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import reset_settings_cache
from app.tenancy.admin import (
    LiveVersionDeleteError,
    NoDraftError,
    VersionConflictError,
    VersionNotFoundError,
    deploy_tenant,
    discard_draft,
    get_draft,
    get_live_version,
    list_versions,
    save_draft,
    switch_to_version,
)
from tests.conftest import mock_http


@pytest.fixture(autouse=True)
def _supabase_configured(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-secret-key")
    reset_settings_cache()
    yield
    reset_settings_cache()


def _draft_row(config: dict | None, updated_at: str | None = None) -> httpx.Response:
    if config is None:
        return httpx.Response(200, json=[{"draft_config": None, "draft_updated_at": None}])
    return httpx.Response(
        200, json=[{"draft_config": config, "draft_updated_at": updated_at or "d1"}]
    )


class TestGetDraft:
    async def test_no_draft_returns_none_none(self, hotel):
        client, _ = mock_http(lambda req: _draft_row(None))
        config, version = await get_draft(hotel.tenant_id, client=client)
        assert config is None
        assert version is None

    async def test_a_stored_draft_round_trips(self, hotel):
        dumped = hotel.model_dump(mode="json")
        client, _ = mock_http(lambda req: _draft_row(dumped, "d1"))
        config, version = await get_draft(hotel.tenant_id, client=client)
        assert config == hotel
        assert version == "d1"

    async def test_no_supabase_configured_returns_none_none_without_a_client(
        self, hotel, monkeypatch
    ):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        reset_settings_cache()
        config, version = await get_draft(hotel.tenant_id)
        assert (config, version) == (None, None)


class TestSaveDraft:
    async def test_writes_draft_config_never_the_live_columns(self, hotel):
        edited = hotel.model_copy(update={"greeting": "Draft greeting"})

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return _draft_row(None)
            return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])

        client, requests = mock_http(handler)
        new_version = await save_draft(edited, expected_version=None, client=client)

        assert new_version  # a timestamp string
        posts = [r for r in requests if r.method == "POST"]
        assert len(posts) == 1
        body = json.loads(posts[0].content)
        assert set(body.keys()) == {"tenant_id", "draft_config", "draft_updated_at"}
        assert body["draft_config"]["greeting"] == "Draft greeting"

    async def test_stale_expected_version_raises_version_conflict(self, hotel):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return _draft_row(hotel.model_dump(mode="json"), "d1")
            return httpx.Response(201, json=[])

        client, requests = mock_http(handler)
        with pytest.raises(VersionConflictError):
            await save_draft(hotel, expected_version="stale", client=client)
        assert not [r for r in requests if r.method == "POST"]

    async def test_matching_expected_version_succeeds(self, hotel):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return _draft_row(hotel.model_dump(mode="json"), "d1")
            return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])

        client, _requests = mock_http(handler)
        result = await save_draft(hotel, expected_version="d1", client=client)
        assert result


class TestDiscardDraft:
    async def test_nulls_out_the_draft_columns(self, hotel):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"tenant_id": hotel.tenant_id}])

        client, requests = mock_http(handler)
        await discard_draft(hotel.tenant_id, client=client)

        patches = [r for r in requests if r.method == "PATCH"]
        assert len(patches) == 1
        body = json.loads(patches[0].content)
        assert body == {"draft_config": None, "draft_updated_at": None}


class TestDeployTenant:
    async def test_no_draft_raises(self, hotel):
        client, _ = mock_http(lambda req: _draft_row(None))
        with pytest.raises(NoDraftError):
            await deploy_tenant(hotel.tenant_id, client=client)

    async def test_deploys_the_draft_writes_live_and_records_a_version(self, hotel):
        edited_dump = {**hotel.model_dump(mode="json"), "greeting": "Deployed greeting"}
        posted_versions: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tenants":
                return _draft_row(edited_dump, "d1")
            if request.method == "GET" and request.url.path == "/tenant_versions":
                return httpx.Response(200, json=[])  # no existing versions
            if request.method == "PATCH" and request.url.path == "/tenant_versions":
                return httpx.Response(200, json=[])
            if request.method == "POST" and request.url.path == "/tenant_versions":
                posted_versions.append(json.loads(request.content))
                return httpx.Response(201, json=[])
            if request.method == "POST" and request.url.path == "/tenants":
                return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])
            if request.method == "PATCH" and request.url.path == "/tenants":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        client, requests = mock_http(handler)
        version = await deploy_tenant(hotel.tenant_id, note="go live", client=client)

        assert version.version_number == 1
        assert version.is_live is True
        assert version.note == "go live"
        assert version.config["greeting"] == "Deployed greeting"
        assert posted_versions and posted_versions[0]["config"]["greeting"] == "Deployed greeting"

        # Draft cleared after a successful deploy.
        draft_clears = [
            r
            for r in requests
            if r.method == "PATCH" and r.url.path == "/tenants" and b"draft_config" in r.content
        ]
        assert draft_clears

    async def test_a_second_deploy_burns_the_next_version_number(self, hotel):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tenants":
                return _draft_row(hotel.model_dump(mode="json"), "d1")
            if request.method == "GET" and request.url.path == "/tenant_versions":
                return httpx.Response(200, json=[{"version_number": 3}])
            return httpx.Response(200, json=[{"tenant_id": hotel.tenant_id}])

        client, _requests = mock_http(handler)
        version = await deploy_tenant(hotel.tenant_id, client=client)
        assert version.version_number == 4

    async def test_an_invalid_stored_draft_raises_a_validation_error(self, hotel):
        from pydantic import ValidationError

        bad = {**hotel.model_dump(mode="json"), "timezone": "Not/ARealZone"}
        client, _ = mock_http(lambda req: _draft_row(bad, "d1"))
        with pytest.raises(ValidationError):
            await deploy_tenant(hotel.tenant_id, client=client)


class TestListVersions:
    async def test_lists_newest_first(self, hotel):
        rows = [
            {
                "id": "v2",
                "tenant_id": hotel.tenant_id,
                "version_number": 2,
                "config": hotel.model_dump(mode="json"),
                "is_live": True,
                "deployed_at": "2026-08-02T00:00:00Z",
            },
            {
                "id": "v1",
                "tenant_id": hotel.tenant_id,
                "version_number": 1,
                "config": hotel.model_dump(mode="json"),
                "is_live": False,
                "deployed_at": "2026-08-01T00:00:00Z",
            },
        ]
        client, _ = mock_http(lambda req: httpx.Response(200, json=rows))
        versions = await list_versions(hotel.tenant_id, client=client)
        assert [v.version_number for v in versions] == [2, 1]
        assert versions[0].is_live is True


class TestSwitchToVersion:
    async def test_unknown_version_raises_not_found(self, hotel):
        client, _ = mock_http(lambda req: httpx.Response(200, json=[]))
        with pytest.raises(VersionNotFoundError):
            await switch_to_version(hotel.tenant_id, "does-not-exist", client=client)

    async def test_switching_writes_live_and_flips_is_live_with_no_new_row(self, hotel):
        version_row = {
            "id": "v1",
            "tenant_id": hotel.tenant_id,
            "version_number": 1,
            "config": hotel.model_dump(mode="json"),
            "is_live": False,
            "deployed_at": "2026-08-01T00:00:00Z",
        }
        posts_to_versions = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tenant_versions":
                return httpx.Response(200, json=[version_row])
            if request.method == "POST" and request.url.path == "/tenant_versions":
                posts_to_versions.append(request)
                return httpx.Response(201, json=[])
            if request.method == "PATCH" and request.url.path == "/tenant_versions":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"tenant_id": hotel.tenant_id}])

        client, _requests = mock_http(handler)
        result = await switch_to_version(hotel.tenant_id, "v1", client=client)

        assert result.is_live is True
        assert result.version_number == 1
        assert not posts_to_versions  # no new row inserted

    async def test_a_version_that_no_longer_validates_raises(self, hotel):
        bad_row = {
            "id": "v1",
            "tenant_id": hotel.tenant_id,
            "version_number": 1,
            "config": {**hotel.model_dump(mode="json"), "timezone": "Not/ARealZone"},
            "is_live": False,
            "deployed_at": "2026-08-01T00:00:00Z",
        }
        from pydantic import ValidationError

        client, _ = mock_http(lambda req: httpx.Response(200, json=[bad_row]))
        with pytest.raises(ValidationError):
            await switch_to_version(hotel.tenant_id, "v1", client=client)


class TestDeleteVersion:
    async def test_unknown_version_raises_not_found(self, hotel):
        from app.tenancy.admin import delete_version

        client, _ = mock_http(lambda req: httpx.Response(200, json=[]))
        with pytest.raises(VersionNotFoundError):
            await delete_version(hotel.tenant_id, "nope", client=client)

    async def test_the_live_version_cannot_be_deleted(self, hotel):
        from app.tenancy.admin import delete_version

        live_row = {
            "id": "v1",
            "tenant_id": hotel.tenant_id,
            "version_number": 1,
            "config": hotel.model_dump(mode="json"),
            "is_live": True,
            "deployed_at": "2026-08-01T00:00:00Z",
        }
        client, requests = mock_http(lambda req: httpx.Response(200, json=[live_row]))
        with pytest.raises(LiveVersionDeleteError):
            await delete_version(hotel.tenant_id, "v1", client=client)
        assert not [r for r in requests if r.method == "DELETE"]

    async def test_a_non_live_version_deletes_cleanly(self, hotel):
        from app.tenancy.admin import delete_version

        row = {
            "id": "v1",
            "tenant_id": hotel.tenant_id,
            "version_number": 1,
            "config": hotel.model_dump(mode="json"),
            "is_live": False,
            "deployed_at": "2026-08-01T00:00:00Z",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=[row])
            return httpx.Response(200, json=[{"id": "v1"}])

        client, requests = mock_http(handler)
        await delete_version(hotel.tenant_id, "v1", client=client)
        assert any(r.method == "DELETE" for r in requests)


class TestGetLiveVersion:
    async def test_no_live_version_returns_none(self, hotel):
        client, _ = mock_http(lambda req: httpx.Response(200, json=[]))
        assert await get_live_version(hotel.tenant_id, client=client) is None

    async def test_returns_the_live_row(self, hotel):
        row = {
            "id": "v1",
            "tenant_id": hotel.tenant_id,
            "version_number": 1,
            "config": hotel.model_dump(mode="json"),
            "is_live": True,
            "deployed_at": "2026-08-01T00:00:00Z",
        }
        client, _ = mock_http(lambda req: httpx.Response(200, json=[row]))
        version = await get_live_version(hotel.tenant_id, client=client)
        assert version is not None
        assert version.is_live is True
