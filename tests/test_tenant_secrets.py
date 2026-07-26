"""app/tenancy/secrets.py — per-tenant Vault secrets, absent vs errored (Phase 4)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import reset_settings_cache
from app.tenancy.secrets import (
    TenantSecretError,
    clear_secret_cache,
    get_tenant_secret,
    resolve_secret,
    set_tenant_secret,
)
from tests.conftest import mock_http


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "test-anon-key")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-jwt-secret")
    reset_settings_cache()
    clear_secret_cache()
    yield
    reset_settings_cache()
    clear_secret_cache()


class TestAbsentVsErrored:
    async def test_no_supabase_configured_is_absent_not_an_error(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        reset_settings_cache()
        result = await get_tenant_secret("hotel-mzv", "calcom_api_key")
        assert result is None

    async def test_null_response_is_absent(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            # httpx.Response(200, json=None) produces an EMPTY body, not the
            # literal `null` PostgREST actually sends for a null RPC result
            # (verified live) — build it explicitly so this test exercises
            # the real wire shape, not httpx's default-value quirk.
            return httpx.Response(
                200, content=b"null", headers={"content-type": "application/json"}
            )

        client, requests = mock_http(handler)
        result = await get_tenant_secret("hotel-mzv", "calcom_api_key", client=client)

        assert result is None
        assert requests[0].url.path == "/rpc/get_tenant_secret"

    async def test_found_secret_is_returned(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json="cal_live_tenant_specific_key")

        client, _ = mock_http(handler)
        result = await get_tenant_secret("hotel-mzv", "calcom_api_key", client=client)
        assert result == "cal_live_tenant_specific_key"

    async def test_5xx_raises_tenant_secret_error(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="vault unreachable")

        client, _ = mock_http(handler)
        with pytest.raises(TenantSecretError):
            await get_tenant_secret("hotel-mzv", "calcom_api_key", client=client)

    async def test_401_raises_tenant_secret_error(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="unauthorized")

        client, _ = mock_http(handler)
        with pytest.raises(TenantSecretError):
            await get_tenant_secret("hotel-mzv", "calcom_api_key", client=client)

    async def test_timeout_raises_tenant_secret_error(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("boom")

        client, _ = mock_http(handler)
        with pytest.raises(TenantSecretError):
            await get_tenant_secret("hotel-mzv", "calcom_api_key", client=client)


class TestResolveSecretFallback:
    async def test_absent_falls_back_to_env_value(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"null", headers={"content-type": "application/json"}
            )

        client, _ = mock_http(handler)
        result = await resolve_secret(
            "northside-plumbing", "calcom_api_key", "cal_shared_env_key", client=client
        )
        assert result == "cal_shared_env_key"

    async def test_found_secret_wins_over_env_value(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json="cal_tenant_own_key")

        client, _ = mock_http(handler)
        result = await resolve_secret(
            "hotel-mzv", "calcom_api_key", "cal_shared_env_key", client=client
        )
        assert result == "cal_tenant_own_key"

    async def test_vault_error_never_falls_back_to_the_shared_env_key(self):
        """The single most important behaviour in this module: a vault error
        must not silently reuse a shared credential that belongs to a
        DIFFERENT tenant's real account."""

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="vault unreachable")

        client, _ = mock_http(handler)
        with pytest.raises(TenantSecretError):
            await resolve_secret(
                "northside-plumbing",
                "calcom_api_key",
                "cal_hotel_mzv_real_account_key",
                client=client,
            )


class TestCaching:
    async def test_repeated_calls_within_ttl_hit_the_network_once(self):
        call_count = 0

        def handler(_req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json="cal_tenant_key")

        client, _ = mock_http(handler)
        first = await get_tenant_secret("hotel-mzv", "calcom_api_key", client=client)
        second = await get_tenant_secret("hotel-mzv", "calcom_api_key", client=client)

        assert first == second == "cal_tenant_key"
        assert call_count == 1

    async def test_different_key_names_are_cached_independently(self):
        seen_bodies = []

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            seen_bodies.append(_json.loads(req.content))
            return httpx.Response(200, json="some-value")

        client, _ = mock_http(handler)
        await get_tenant_secret("hotel-mzv", "calcom_api_key", client=client)
        await get_tenant_secret("hotel-mzv", "twilio_auth_token", client=client)

        assert len(seen_bodies) == 2
        assert {b["key_name"] for b in seen_bodies} == {"calcom_api_key", "twilio_auth_token"}

    async def test_clear_secret_cache_forces_a_refetch(self):
        call_count = 0

        def handler(_req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json="cal_tenant_key")

        client, _ = mock_http(handler)
        await get_tenant_secret("hotel-mzv", "calcom_api_key", client=client)
        clear_secret_cache()
        await get_tenant_secret("hotel-mzv", "calcom_api_key", client=client)

        assert call_count == 2

    async def test_stale_cache_is_served_when_the_vault_errors(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("SECRET_CACHE_TTL_SECONDS", "0")
        reset_settings_cache()
        assert get_settings().secret_cache_ttl_seconds == 0

        responses = [httpx.Response(200, json="cal_tenant_key"), httpx.Response(500, text="down")]

        def handler(_req: httpx.Request) -> httpx.Response:
            return responses.pop(0)

        client, _ = mock_http(handler)
        first = await get_tenant_secret("hotel-mzv", "calcom_api_key", client=client)
        assert first == "cal_tenant_key"

        # TTL is 0, so the second call re-fetches, hits the 500, and must
        # fall back to the stale cached value rather than raising.
        second = await get_tenant_secret("hotel-mzv", "calcom_api_key", client=client)
        assert second == "cal_tenant_key"


class TestSetTenantSecret:
    async def test_request_shape(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(204)

        client, requests = mock_http(handler)
        await set_tenant_secret("hotel-mzv", "calcom_api_key", "cal_new_key", client=client)

        assert requests[0].url.path == "/rpc/set_tenant_secret"
        body = json.loads(requests[0].content)
        assert body == {
            "tenant_id": "hotel-mzv",
            "key_name": "calcom_api_key",
            "secret_value": "cal_new_key",
        }

    async def test_missing_admin_config_raises_without_http_call(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        reset_settings_cache()
        with pytest.raises(TenantSecretError):
            await set_tenant_secret("hotel-mzv", "calcom_api_key", "cal_new_key")

    async def test_error_response_raises(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="permission denied")

        client, _ = mock_http(handler)
        with pytest.raises(TenantSecretError):
            await set_tenant_secret("hotel-mzv", "calcom_api_key", "cal_new_key", client=client)
