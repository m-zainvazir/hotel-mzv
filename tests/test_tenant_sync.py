"""app/tenancy/sync.py — pushing tenant JSON into Supabase (Phase 4 Step 9).

No network — every client is httpx.MockTransport via mock_http().
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import reset_settings_cache
from app.tenancy.sync import TenantSyncError, sync_tenant
from tests.conftest import mock_http


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-secret-key")
    reset_settings_cache()
    yield
    reset_settings_cache()


_PREFER_HEADER = {"Prefer": "resolution=merge-duplicates,return=representation"}


class TestSyncTenant:
    async def test_tenant_row_request_shape(self, hotel):
        seen = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            if req.url.path == "/tenants":
                return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])
            return httpx.Response(201, json=[])

        # An injected client carries whatever headers the test gives it
        # (mirrors test_calcom_booking.py's `_mock_calcom` convention) —
        # sync_tenant only sets Prefer when it builds its OWN client, which
        # injection deliberately bypasses.
        client, requests = mock_http(handler, headers=_PREFER_HEADER)
        await sync_tenant(hotel, client=client)

        tenant_reqs = [r for r in requests if r.url.path == "/tenants"]
        assert len(tenant_reqs) == 1
        req = tenant_reqs[0]
        assert dict(req.url.params)["on_conflict"] == "tenant_id"
        assert "merge-duplicates" in req.headers["prefer"]
        assert "return=representation" in req.headers["prefer"]

        body = json.loads(req.content)
        assert body["tenant_id"] == hotel.tenant_id
        assert body["name"] == hotel.name
        assert "services" not in body
        assert "tenant_id" not in body["config"]
        assert "greeting" in body["config"]
        assert "booking" in body["config"]
        assert "voice" in body["config"]

    async def test_services_batch_request_shape(self, hotel):
        requests_seen = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_seen.append(req)
            if req.url.path == "/tenants":
                return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])
            return httpx.Response(201, json=[{"slug": "x"}])

        client, requests = mock_http(handler)
        await sync_tenant(hotel, client=client)

        # Phase 8: sync_tenant now also deletes orphaned services (any slug
        # the tenant no longer declares), so "/services" sees two requests —
        # the upsert (POST) and the delete-of-absent (DELETE).
        service_posts = [r for r in requests if r.url.path == "/services" and r.method == "POST"]
        assert len(service_posts) == 1
        req = service_posts[0]
        assert dict(req.url.params)["on_conflict"] == "tenant_id,slug"

        body = json.loads(req.content)
        assert len(body) == len(hotel.services)
        assert all(row["tenant_id"] == hotel.tenant_id for row in body)
        assert {row["slug"] for row in body} == {s.slug for s in hotel.services}

    async def test_deletes_services_and_mcp_servers_absent_from_the_current_config(self, hotel):
        """The bug this closes: sync_tenant used to only ever upsert, so
        removing a service or MCP server from a tenant's config left an
        orphan row forever — check_availability would keep offering a
        service the tenant no longer declares, and MCP_SOURCE=supabase would
        keep loading a server that was supposedly removed."""

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/tenants":
                return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])
            return httpx.Response(200, json=[])

        client, requests = mock_http(handler)
        await sync_tenant(hotel, client=client)

        services_delete = next(
            r for r in requests if r.url.path == "/services" and r.method == "DELETE"
        )
        params = dict(services_delete.url.params)
        assert params["tenant_id"] == f"eq.{hotel.tenant_id}"
        current_slugs = {s.slug for s in hotel.services}
        sent_slugs = params["slug"].removeprefix("not.in.(").removesuffix(")").split(",")
        assert current_slugs <= set(sent_slugs)

        mcp_delete = next(
            r for r in requests if r.url.path == "/mcp_servers" and r.method == "DELETE"
        )
        mcp_params = dict(mcp_delete.url.params)
        assert mcp_params["tenant_id"] == f"eq.{hotel.tenant_id}"
        current_names = {s.name for s in hotel.mcp_servers}
        assert current_names <= set(
            mcp_params["name"].removeprefix("not.in.(").removesuffix(")").split(",")
        )

    async def test_deletes_every_row_when_the_tenant_has_none_left(self):
        """An empty services/mcp_servers list means "delete every row for
        this tenant", not "skip the delete" — `not.in.()` is invalid
        PostgREST syntax for an empty list, so this must take the
        no-filter branch instead."""
        from app.tenancy.models import EmergencyPolicy, TenantConfig

        bare_tenant = TenantConfig(
            tenant_id="bare-test-tenant",
            name="Bare",
            trade="test",
            greeting="hi",
            emergency=EmergencyPolicy(escalation_phone="+15550000000"),
        )

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json=[{"tenant_id": "bare-test-tenant"}])

        client, requests = mock_http(handler)
        await sync_tenant(bare_tenant, client=client)

        services_delete = next(r for r in requests if r.url.path == "/services")
        assert services_delete.method == "DELETE"
        assert "slug" not in dict(services_delete.url.params)

        mcp_delete = next(r for r in requests if r.url.path == "/mcp_servers")
        assert mcp_delete.method == "DELETE"
        assert "name" not in dict(mcp_delete.url.params)

    async def test_tenant_with_no_services_skips_the_services_upsert_post(self):
        """Renamed from "skips the services call" (Phase 8): sync_tenant now
        always issues a DELETE for absent children, even with zero services
        — see test_deletes_every_row_when_the_tenant_has_none_left above.
        What genuinely still gets skipped is the upsert POST, since there's
        nothing to upsert."""
        from app.tenancy.models import EmergencyPolicy, TenantConfig

        bare_tenant = TenantConfig(
            tenant_id="bare-test-tenant",
            name="Bare",
            trade="test",
            greeting="hi",
            emergency=EmergencyPolicy(escalation_phone="+15550000000"),
        )

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json=[{"tenant_id": "bare-test-tenant"}])

        client, requests = mock_http(handler)
        await sync_tenant(bare_tenant, client=client)

        assert not [r for r in requests if r.url.path == "/services" and r.method == "POST"]
        assert not [r for r in requests if r.url.path == "/mcp_servers" and r.method == "POST"]

    async def test_missing_admin_config_raises_without_http_call(self, hotel, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        reset_settings_cache()
        with pytest.raises(TenantSyncError):
            await sync_tenant(hotel)

    async def test_tenant_row_error_raises(self, hotel):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="db error")

        client, _ = mock_http(handler)
        with pytest.raises(TenantSyncError):
            await sync_tenant(hotel, client=client)

    async def test_services_error_raises(self, hotel):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/tenants":
                return httpx.Response(201, json=[{"tenant_id": hotel.tenant_id}])
            return httpx.Response(500, text="db error")

        client, _ = mock_http(handler)
        with pytest.raises(TenantSyncError):
            await sync_tenant(hotel, client=client)
