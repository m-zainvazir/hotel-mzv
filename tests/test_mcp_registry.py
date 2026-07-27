"""app/mcp/registry.py — which MCP servers a tenant may use (Phase 6).

Offline via mock_http() + monkeypatched settings, matching the pattern in
tests/test_supabase_store.py / test_tenant_sync.py. Any registry failure
must return [] and log a WARNING — never raise (see registry.py's
module docstring for why that's the opposite posture to
app/tenancy/secrets.py's vault handling).
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.config import reset_settings_cache
from app.mcp.registry import servers_for
from app.tenancy.models import McpServerConfig
from tests.conftest import mock_http


def _server(*, name: str = "demo", enabled: bool = True) -> McpServerConfig:
    return McpServerConfig(name=name, enabled=enabled, url="https://example.invalid/mcp")


async def test_json_source_is_the_default(hotel):
    tenant = hotel.model_copy(update={"mcp_servers": [_server()]})
    result = await servers_for(tenant)
    assert [s.name for s in result] == ["demo"]


async def test_json_source_filters_to_enabled(hotel):
    tenant = hotel.model_copy(
        update={
            "mcp_servers": [
                _server(name="on", enabled=True),
                _server(name="off", enabled=False),
            ]
        }
    )
    result = await servers_for(tenant)
    assert [s.name for s in result] == ["on"]


@pytest.fixture
def supabase_source(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-jwt-secret")
    monkeypatch.setenv("MCP_SOURCE", "supabase")
    reset_settings_cache()
    yield
    reset_settings_cache()


class TestSupabaseSource:
    async def test_request_carries_tenant_filter_enabled_filter_and_jwt(
        self, hotel, supabase_source
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "tavily",
                        "transport": "http",
                        "url": "https://mcp.tavily.com/mcp/",
                        "auth_secret_ref": "TAVILY_API_KEY",
                    }
                ],
            )

        client, requests = mock_http(handler)
        result = await servers_for(hotel, client=client)

        assert len(requests) == 1
        req = requests[0]
        assert req.url.path == "/mcp_servers"
        params = dict(req.url.params)
        assert params["tenant_id"] == f"eq.{hotel.tenant_id}"
        assert params["enabled"] == "is.true"
        assert req.headers["authorization"].startswith("Bearer ")
        assert [s.name for s in result] == ["tavily"]

    async def test_no_project_configured_returns_empty_no_error(self, hotel, monkeypatch):
        monkeypatch.setenv("MCP_SOURCE", "supabase")
        reset_settings_cache()
        try:
            result = await servers_for(hotel)
        finally:
            reset_settings_cache()
        assert result == []

    async def test_missing_jwt_secret_returns_empty_and_logs(self, hotel, monkeypatch, caplog):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("MCP_SOURCE", "supabase")
        reset_settings_cache()
        try:
            with caplog.at_level(logging.WARNING):
                result = await servers_for(hotel)
        finally:
            reset_settings_cache()
        assert result == []
        assert any("SUPABASE_JWT_SECRET" in r.message for r in caplog.records)

    async def test_5xx_returns_empty_and_logs_never_raises(self, hotel, supabase_source, caplog):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="db exploded")

        client, _ = mock_http(handler)
        with caplog.at_level(logging.WARNING):
            result = await servers_for(hotel, client=client)

        assert result == []
        assert any("mcp registry" in r.message for r in caplog.records)

    async def test_transport_error_returns_empty_and_logs_never_raises(
        self, hotel, supabase_source, caplog
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        client, _ = mock_http(handler)
        with caplog.at_level(logging.WARNING):
            result = await servers_for(hotel, client=client)

        assert result == []
        assert any("mcp registry" in r.message for r in caplog.records)

    async def test_malformed_row_is_skipped_not_fatal(self, hotel, supabase_source, caplog):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {"name": "not a legal name!", "transport": "http", "url": "https://x"},
                    {"name": "good", "transport": "http", "url": "https://y"},
                ],
            )

        client, _ = mock_http(handler)
        with caplog.at_level(logging.WARNING):
            result = await servers_for(hotel, client=client)

        assert [s.name for s in result] == ["good"]

    async def test_empty_body_returns_empty_list(self, hotel, supabase_source):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"")

        client, _ = mock_http(handler)
        result = await servers_for(hotel, client=client)
        assert result == []
