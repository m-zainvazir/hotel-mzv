"""app/mcp/connections.py — McpServerConfig -> MultiServerMCPClient connection
dict (Phase 6).

No network: `resolve_secret` is exercised directly for the happy path (no
Vault configured -> `env_value` fallback, which is None here, so these tests
either supply no `auth_secret_ref` or monkeypatch `resolve_secret` itself)
rather than standing up a fake Supabase — that machinery is already covered
by tests/test_tenant_secrets.py.
"""

from __future__ import annotations

import logging

import pytest

from app.config import reset_settings_cache
from app.mcp.connections import build_connection, redacted
from app.tenancy.models import McpServerConfig
from app.tenancy.secrets import TenantSecretError


async def test_http_transport_becomes_streamable_http():
    server = McpServerConfig(name="demo", url="https://example.invalid/mcp")
    connection = await build_connection("hotel-mzv", server)
    assert connection["transport"] == "streamable_http"
    assert connection["url"] == "https://example.invalid/mcp"


async def test_stdio_refused_by_default(caplog):
    server = McpServerConfig(name="demo", transport="stdio", command="python", args=["x.py"])
    with caplog.at_level(logging.WARNING):
        connection = await build_connection("hotel-mzv", server)
    assert connection is None
    assert any("MCP_ALLOW_STDIO" in r.message for r in caplog.records)


async def test_stdio_allowed_when_flag_set(monkeypatch):
    monkeypatch.setenv("MCP_ALLOW_STDIO", "true")
    reset_settings_cache()
    try:
        server = McpServerConfig(name="demo", transport="stdio", command="python", args=["x.py"])
        connection = await build_connection("hotel-mzv", server)
    finally:
        reset_settings_cache()
    assert connection == {"transport": "stdio", "command": "python", "args": ["x.py"]}


async def test_secret_substituted_into_url_and_headers(monkeypatch):
    async def _fake_resolve(tenant_id, key_name, *, env_value=None, client=None):
        assert key_name == "TAVILY_API_KEY"
        return "tvly-xxxxx"

    monkeypatch.setattr("app.mcp.connections.resolve_secret", _fake_resolve)

    server = McpServerConfig(
        name="tavily",
        url="https://mcp.tavily.com/mcp/?tavilyApiKey=${secret}",
        headers={"X-Extra": "prefix-${secret}-suffix"},
        auth_secret_ref="TAVILY_API_KEY",
    )
    connection = await build_connection("hotel-mzv", server)

    assert connection["url"] == "https://mcp.tavily.com/mcp/?tavilyApiKey=tvly-xxxxx"
    assert connection["headers"] == {"X-Extra": "prefix-tvly-xxxxx-suffix"}


async def test_bearer_default_when_headers_empty(monkeypatch):
    async def _fake_resolve(tenant_id, key_name, *, env_value=None, client=None):
        return "sk_live_abc"

    monkeypatch.setattr("app.mcp.connections.resolve_secret", _fake_resolve)

    server = McpServerConfig(
        name="acme", url="https://acme.example/mcp", auth_secret_ref="ACME_TOKEN"
    )
    connection = await build_connection("hotel-mzv", server)

    assert connection["headers"] == {"Authorization": "Bearer sk_live_abc"}


async def test_secret_error_skips_the_server_returns_none(monkeypatch, caplog):
    async def _raise(*_args, **_kwargs):
        raise TenantSecretError("vault unreachable")

    monkeypatch.setattr("app.mcp.connections.resolve_secret", _raise)

    server = McpServerConfig(
        name="acme", url="https://acme.example/mcp", auth_secret_ref="ACME_TOKEN"
    )
    with caplog.at_level(logging.WARNING):
        connection = await build_connection("hotel-mzv", server)

    assert connection is None
    assert any("could not resolve secret" in r.message for r in caplog.records)


async def test_missing_secret_value_skips_rather_than_connecting_unauthenticated(caplog):
    # No Supabase configured (hermetic settings) -> resolve_secret's real
    # implementation returns None (env_value) rather than raising.
    server = McpServerConfig(
        name="acme", url="https://acme.example/mcp", auth_secret_ref="ACME_TOKEN"
    )
    with caplog.at_level(logging.WARNING):
        connection = await build_connection("hotel-mzv", server)

    assert connection is None
    assert any("no value in Vault" in r.message for r in caplog.records)


def test_redacted_hides_query_string_and_headers():
    connection = {
        "transport": "streamable_http",
        "url": "https://mcp.tavily.com/mcp/?tavilyApiKey=tvly-super-secret",
        "headers": {"Authorization": "Bearer sk_live_abc"},
    }
    safe = redacted(connection)

    assert "tvly-super-secret" not in safe["url"]
    assert safe["url"] == "https://mcp.tavily.com/mcp/?***"
    assert safe["headers"] == {"Authorization": "***"}


def test_redacted_leaves_a_url_with_no_query_string_alone():
    safe = redacted({"url": "https://acme.example/mcp", "headers": {}})
    assert safe["url"] == "https://acme.example/mcp"


@pytest.mark.parametrize("bad_name", ["My Server", "server with spaces", "UPPER", "a" * 33, ""])
def test_illegal_server_name_fails_at_config_load(bad_name):
    with pytest.raises(ValueError):
        McpServerConfig(name=bad_name, url="https://example.invalid/mcp")


def test_http_transport_requires_url():
    with pytest.raises(ValueError):
        McpServerConfig(name="demo", transport="http")


def test_stdio_transport_requires_command():
    with pytest.raises(ValueError):
        McpServerConfig(name="demo", transport="stdio")
