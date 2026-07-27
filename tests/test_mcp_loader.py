"""app/mcp/client.py — the per-tenant MCP tool loader (Phase 6).

`langchain_mcp_adapters.client.MultiServerMCPClient` is monkeypatched to the
fake in tests/_mcp_fakes.py throughout — this suite never touches a real
network or a real MCP server. See that module's docstring for what the fake
proves: its constructor call count is a direct proxy for "was the cache
actually hit".
"""

from __future__ import annotations

import logging
import sys

import pytest

from app.config import reset_settings_cache
from app.mcp.client import clear_mcp_cache, load_mcp_tools
from tests._mcp_fakes import make_fake_client


def _server(name: str = "demo", **overrides):
    from app.tenancy.models import McpServerConfig

    overrides.setdefault("url", f"https://example.invalid/{name}")
    return McpServerConfig(name=name, **overrides)


@pytest.fixture
def mcp_enabled(monkeypatch):
    monkeypatch.setenv("MCP_ENABLED", "true")
    reset_settings_cache()
    yield
    reset_settings_cache()


async def test_disabled_short_circuits_before_any_registry_call(hotel, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("servers_for must not be called when MCP_ENABLED is false")

    monkeypatch.setattr("app.mcp.client.servers_for", _boom)
    tenant = hotel.model_copy(update={"mcp_servers": [_server()]})

    result = await load_mcp_tools(tenant)

    assert result == []


async def test_no_servers_returns_empty(hotel, mcp_enabled):
    result = await load_mcp_tools(hotel)
    assert result == []


async def test_tools_are_loaded_and_prefixed(hotel, mcp_enabled, monkeypatch):
    FakeClient, calls = make_fake_client(server_tools={"demo": ["search", "extract"]})
    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)

    tenant = hotel.model_copy(update={"mcp_servers": [_server()]})
    tools = await load_mcp_tools(tenant)

    assert {t.name for t in tools} == {"demo_search", "demo_extract"}
    assert calls["constructed"] == 1


async def test_cache_hit_within_and_across_calls(hotel, mcp_enabled, monkeypatch):
    FakeClient, calls = make_fake_client(server_tools={"demo": ["search"]})
    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)

    tenant = hotel.model_copy(update={"mcp_servers": [_server()]})
    first = await load_mcp_tools(tenant)
    second = await load_mcp_tools(tenant)

    assert calls["constructed"] == 1
    assert [t.name for t in first] == [t.name for t in second]


async def test_ttl_expiry_forces_a_refetch(hotel, mcp_enabled, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr("app.mcp.client.time.monotonic", lambda: clock["now"])
    monkeypatch.setenv("MCP_TOOL_CACHE_TTL_SECONDS", "10")
    reset_settings_cache()

    FakeClient, calls = make_fake_client(server_tools={"demo": ["search"]})
    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)

    tenant = hotel.model_copy(update={"mcp_servers": [_server()]})
    await load_mcp_tools(tenant)
    clock["now"] += 11
    await load_mcp_tools(tenant)

    assert calls["constructed"] == 2


async def test_fingerprint_change_invalidates_even_within_ttl(hotel, mcp_enabled, monkeypatch):
    FakeClient, calls = make_fake_client(server_tools={"demo": ["search"]})
    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)

    tenant_a = hotel.model_copy(update={"mcp_servers": [_server(url="https://example.invalid/a")]})
    tenant_b = hotel.model_copy(update={"mcp_servers": [_server(url="https://example.invalid/b")]})

    await load_mcp_tools(tenant_a)
    await load_mcp_tools(tenant_b)  # same tenant_id, different resolved connection

    assert calls["constructed"] == 2


async def test_a_hanging_server_times_out_without_blocking_the_others(
    hotel, mcp_enabled, monkeypatch
):
    monkeypatch.setenv("MCP_CONNECT_TIMEOUT_SECONDS", "0.05")
    reset_settings_cache()

    FakeClient, _calls = make_fake_client(
        server_tools={"fast": ["search"], "slow": ["ignored"]}, hang=("slow",)
    )
    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)

    tenant = hotel.model_copy(
        update={
            "mcp_servers": [
                _server(name="fast"),
                _server(name="slow"),
            ]
        }
    )
    tools = await load_mcp_tools(tenant)

    assert {t.name for t in tools} == {"fast_search"}


async def test_a_raising_server_is_skipped_without_killing_the_others(
    hotel, mcp_enabled, monkeypatch, caplog
):
    FakeClient, _calls = make_fake_client(
        server_tools={"good": ["search"], "bad": ["ignored"]}, raise_for=("bad",)
    )
    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)

    tenant = hotel.model_copy(update={"mcp_servers": [_server(name="good"), _server(name="bad")]})
    with caplog.at_level(logging.WARNING):
        tools = await load_mcp_tools(tenant)

    assert {t.name for t in tools} == {"good_search"}
    assert any("bad" in r.message for r in caplog.records)


async def test_tool_allowlist_filters_to_named_tools(hotel, mcp_enabled, monkeypatch):
    FakeClient, _calls = make_fake_client(server_tools={"demo": ["search", "extract", "crawl"]})
    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)

    tenant = hotel.model_copy(update={"mcp_servers": [_server(tool_allowlist=["search"])]})
    tools = await load_mcp_tools(tenant)

    assert [t.name for t in tools] == ["demo_search"]


async def test_max_tools_truncates_and_warns(hotel, mcp_enabled, monkeypatch, caplog):
    monkeypatch.setenv("MCP_MAX_TOOLS", "3")
    reset_settings_cache()

    FakeClient, _calls = make_fake_client(server_tools={"demo": ["a", "b", "c", "d", "e"]})
    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)

    tenant = hotel.model_copy(update={"mcp_servers": [_server()]})
    with caplog.at_level(logging.WARNING):
        tools = await load_mcp_tools(tenant)

    assert len(tools) == 3
    assert any("truncating" in r.message for r in caplog.records)


async def test_missing_adapter_dependency_degrades_to_empty(
    hotel, mcp_enabled, monkeypatch, caplog
):
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", None)

    tenant = hotel.model_copy(update={"mcp_servers": [_server()]})
    with caplog.at_level(logging.WARNING):
        result = await load_mcp_tools(tenant)

    assert result == []
    assert any("not installed" in r.message for r in caplog.records)


async def test_clear_mcp_cache_hook(hotel, mcp_enabled, monkeypatch):
    FakeClient, calls = make_fake_client(server_tools={"demo": ["search"]})
    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)

    tenant = hotel.model_copy(update={"mcp_servers": [_server()]})
    await load_mcp_tools(tenant)
    clear_mcp_cache()
    await load_mcp_tools(tenant)

    assert calls["constructed"] == 2
