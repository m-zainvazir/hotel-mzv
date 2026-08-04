"""app/mcp/oauth.py — Cal.com hosted MCP OAuth 2.1 (Phase 9 Part A).

No network: `discover` / `register_client` / `exchange_code` / the headless
refresh in `access_token_for` all accept an injected `client=`, matching the
house convention every other provider in this codebase uses — every test
here builds one with `mock_http()` rather than hitting mcp.cal.com.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from app.mcp.connections import build_connection, redacted
from app.mcp.oauth import (
    CalcomOAuthError,
    OAuthMetadata,
    access_token_for,
    build_authorize_url,
    discover,
    exchange_code,
    generate_pkce_pair,
    register_client,
)
from app.tenancy.models import McpServerConfig
from tests.conftest import mock_http

_DISCOVERY_RESPONSES = {
    "/.well-known/oauth-protected-resource": {
        "resource": "https://mcp.cal.com",
        "authorization_servers": ["https://mcp.cal.com"],
        "bearer_methods_supported": ["header"],
    },
    "/.well-known/oauth-authorization-server": {
        "issuer": "https://mcp.cal.com",
        "authorization_endpoint": "https://mcp.cal.com/oauth/authorize",
        "token_endpoint": "https://mcp.cal.com/oauth/token",
        "registration_endpoint": "https://mcp.cal.com/oauth/register",
        "revocation_endpoint": "https://mcp.cal.com/oauth/revoke",
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    },
}

_METADATA = OAuthMetadata(
    authorization_endpoint="https://mcp.cal.com/oauth/authorize",
    token_endpoint="https://mcp.cal.com/oauth/token",
    registration_endpoint="https://mcp.cal.com/oauth/register",
)


def _discovery_handler(request: httpx.Request) -> httpx.Response:
    body = _DISCOVERY_RESPONSES.get(request.url.path)
    if body is None:
        raise AssertionError(f"unexpected discovery request: {request.url.path}")
    return httpx.Response(200, json=body)


def _form_body(request: httpx.Request) -> dict[str, str]:
    parsed = parse_qs(request.content.decode("utf-8"))
    return {key: values[0] for key, values in parsed.items()}


# --- PKCE --------------------------------------------------------------


def test_pkce_challenge_is_s256_of_verifier():
    pair = generate_pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(pair.verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert pair.challenge == expected
    assert pair.verifier != pair.challenge


def test_pkce_values_are_unpadded_urlsafe_base64():
    pair = generate_pkce_pair()
    assert "=" not in pair.verifier
    assert "=" not in pair.challenge
    assert "+" not in pair.verifier and "/" not in pair.verifier


def test_pkce_pairs_are_not_reused():
    a = generate_pkce_pair()
    b = generate_pkce_pair()
    assert a.verifier != b.verifier


# --- discovery + DCR + authorize URL ------------------------------------


class TestDiscover:
    async def test_two_hop_discovery(self):
        client, requests = mock_http(_discovery_handler)
        metadata = await discover("https://mcp.cal.com/mcp", client=client)

        assert metadata.authorization_endpoint == "https://mcp.cal.com/oauth/authorize"
        assert metadata.token_endpoint == "https://mcp.cal.com/oauth/token"
        assert metadata.registration_endpoint == "https://mcp.cal.com/oauth/register"
        assert [r.url.path for r in requests] == [
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
        ]

    async def test_missing_required_field_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/.well-known/oauth-protected-resource":
                return httpx.Response(200, json={"authorization_servers": ["https://mcp.cal.com"]})
            return httpx.Response(200, json={"issuer": "https://mcp.cal.com"})  # no endpoints

        client, _requests = mock_http(handler)
        with pytest.raises(CalcomOAuthError):
            await discover("https://mcp.cal.com/mcp", client=client)

    async def test_transport_failure_raises_calcom_oauth_error(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        client, _requests = mock_http(handler)
        with pytest.raises(CalcomOAuthError):
            await discover("https://mcp.cal.com/mcp", client=client)


class TestRegisterClient:
    async def test_dcr_posts_redirect_uri_and_returns_public_client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["redirect_uris"] == ["http://127.0.0.1:8901/callback"]
            assert payload["token_endpoint_auth_method"] == "none"
            return httpx.Response(
                201,
                json={
                    "client_id": "c1424e5e-db4f-486e-b54b-23c0b25ecbfa",
                    "redirect_uris": ["http://127.0.0.1:8901/callback"],
                },
            )

        client, requests = mock_http(handler)
        registration = await register_client(
            _METADATA, redirect_uri="http://127.0.0.1:8901/callback", client=client
        )

        assert registration.client_id == "c1424e5e-db4f-486e-b54b-23c0b25ecbfa"
        assert registration.client_secret is None
        assert len(requests) == 1

    async def test_no_registration_endpoint_raises(self):
        metadata = OAuthMetadata(
            authorization_endpoint="https://mcp.cal.com/oauth/authorize",
            token_endpoint="https://mcp.cal.com/oauth/token",
            registration_endpoint=None,
        )
        with pytest.raises(CalcomOAuthError):
            await register_client(metadata, redirect_uri="http://127.0.0.1:8901/callback")

    async def test_rejected_registration_raises(self):
        client, _requests = mock_http(lambda req: httpx.Response(400, json={"error": "bad"}))
        with pytest.raises(CalcomOAuthError):
            await register_client(
                _METADATA, redirect_uri="http://127.0.0.1:8901/callback", client=client
            )


def test_build_authorize_url_has_pkce_and_state():
    url = build_authorize_url(
        _METADATA,
        client_id="client_123",
        redirect_uri="http://127.0.0.1:8901/callback",
        code_challenge="challenge-abc",
        state="state-xyz",
    )
    assert url.startswith("https://mcp.cal.com/oauth/authorize?")
    assert "code_challenge=challenge-abc" in url
    assert "code_challenge_method=S256" in url
    assert "state=state-xyz" in url
    assert "response_type=code" in url


class TestExchangeCode:
    async def test_posts_pkce_verifier_and_returns_tokens(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = _form_body(request)
            assert body["grant_type"] == "authorization_code"
            assert body["code"] == "auth-code-1"
            assert body["code_verifier"] == "verifier-1"
            assert "client_secret" not in body  # public client
            return httpx.Response(
                200, json={"access_token": "at_1", "refresh_token": "rt_1", "expires_in": 3600}
            )

        client, _requests = mock_http(handler)
        tokens = await exchange_code(
            _METADATA,
            code="auth-code-1",
            redirect_uri="http://127.0.0.1:8901/callback",
            client_id="client_123",
            client_secret=None,
            code_verifier="verifier-1",
            client=client,
        )
        assert tokens["access_token"] == "at_1"
        assert tokens["refresh_token"] == "rt_1"

    async def test_rejected_exchange_raises(self):
        client, _requests = mock_http(
            lambda req: httpx.Response(400, json={"error": "invalid_grant"})
        )
        with pytest.raises(CalcomOAuthError):
            await exchange_code(
                _METADATA,
                code="bad-code",
                redirect_uri="http://127.0.0.1:8901/callback",
                client_id="client_123",
                client_secret=None,
                code_verifier="verifier-1",
                client=client,
            )


# --- headless refresh ----------------------------------------------------

_VAULT_VALUES = {
    "calcom_mcp_refresh_token": "rt_stored",
    "calcom_mcp_client_id": "client_stored",
    "calcom_mcp_client_secret": None,
}


async def _fake_resolve_secret(tenant_id: str, key_name: str, *args: object, **kwargs: object):
    return _VAULT_VALUES.get(key_name)


class TestAccessTokenFor:
    async def test_no_grant_raises_naming_the_authorize_command(self):
        # Hermetic settings (no SUPABASE_URL) -> resolve_secret's real
        # implementation returns None for every key without any network call.
        with pytest.raises(CalcomOAuthError) as exc_info:
            await access_token_for("hotel-mzv")
        assert "authorize_calcom" in str(exc_info.value)
        assert "hotel-mzv" in str(exc_info.value)

    async def test_refresh_exchange_and_cache_hit(self, monkeypatch):
        monkeypatch.setattr("app.mcp.oauth.resolve_secret", _fake_resolve_secret)
        token_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                token_requests.append(request)
                body = _form_body(request)
                assert body["grant_type"] == "refresh_token"
                assert body["refresh_token"] == "rt_stored"
                assert body["client_id"] == "client_stored"
                assert "client_secret" not in body
                return httpx.Response(200, json={"access_token": "at_fresh", "expires_in": 3600})
            return _discovery_handler(request)

        client, _requests = mock_http(handler)

        token = await access_token_for("hotel-mzv", client=client)
        assert token == "at_fresh"
        assert len(token_requests) == 1

        # Cached — a second call within the TTL must not hit the token
        # endpoint again.
        token_again = await access_token_for("hotel-mzv", client=client)
        assert token_again == "at_fresh"
        assert len(token_requests) == 1

    async def test_expired_cache_entry_triggers_a_fresh_refresh(self, monkeypatch):
        import app.mcp.oauth as oauth_mod

        monkeypatch.setattr("app.mcp.oauth.resolve_secret", _fake_resolve_secret)
        oauth_mod._cache["hotel-mzv"] = (time.time() - 1, "stale-token")

        token_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                token_requests.append(request)
                return httpx.Response(200, json={"access_token": "at_new", "expires_in": 3600})
            return _discovery_handler(request)

        client, _requests = mock_http(handler)
        token = await access_token_for("hotel-mzv", client=client)

        assert token == "at_new"
        assert len(token_requests) == 1

    async def test_rotated_refresh_token_is_persisted(self, monkeypatch):
        stored: dict[str, str] = {}

        async def fake_set_secret(tenant_id, key_name, value, *, client=None):
            stored[key_name] = value

        monkeypatch.setattr("app.mcp.oauth.resolve_secret", _fake_resolve_secret)
        monkeypatch.setattr("app.mcp.oauth.set_tenant_secret", fake_set_secret)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                return httpx.Response(
                    200,
                    json={
                        "access_token": "at_1",
                        "refresh_token": "rt_rotated",
                        "expires_in": 3600,
                    },
                )
            return _discovery_handler(request)

        client, _requests = mock_http(handler)
        await access_token_for("hotel-mzv", client=client)

        assert stored["calcom_mcp_refresh_token"] == "rt_rotated"

    async def test_revoked_grant_raises_calcom_oauth_error(self, monkeypatch):
        monkeypatch.setattr("app.mcp.oauth.resolve_secret", _fake_resolve_secret)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return _discovery_handler(request)

        client, _requests = mock_http(handler)
        with pytest.raises(CalcomOAuthError):
            await access_token_for("hotel-mzv", client=client)


# --- redaction -------------------------------------------------------------


async def test_oauth_access_token_never_appears_in_a_redacted_connection(monkeypatch):
    async def fake_access_token_for(tenant_id: str, *, client=None):
        return "super-secret-access-token"

    monkeypatch.setattr("app.mcp.oauth.access_token_for", fake_access_token_for)

    server = McpServerConfig(name="calcom", url="https://mcp.cal.com/mcp", auth="oauth")
    connection = await build_connection("hotel-mzv", server)

    assert connection is not None
    assert connection["headers"]["Authorization"] == "Bearer super-secret-access-token"

    safe = redacted(connection)
    assert "super-secret-access-token" not in json.dumps(safe)
    assert safe["headers"] == {"Authorization": "***"}


async def test_oauth_resolution_failure_skips_the_server_returns_none(monkeypatch, caplog):
    async def fake_access_token_for(tenant_id: str, *, client=None):
        raise CalcomOAuthError("no grant")

    monkeypatch.setattr("app.mcp.oauth.access_token_for", fake_access_token_for)

    server = McpServerConfig(name="calcom", url="https://mcp.cal.com/mcp", auth="oauth")
    connection = await build_connection("hotel-mzv", server)

    assert connection is None
