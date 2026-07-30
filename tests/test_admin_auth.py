"""Admin auth — the abstraction the "tenant login later" bet rests on
(Phase 8). Exercised through a tiny throwaway FastAPI app rather than by
calling the dependency functions directly, so 401/403/429 status codes and
headers are asserted the way a real client would see them.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.channels import admin_auth
from app.channels.admin_auth import AdminPrincipal, require_admin, require_tenant_access
from app.config import get_settings, reset_settings_cache

_TOKEN = "s3cret-admin-token-that-is-long-enough-to-pass-preflight"


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(principal: AdminPrincipal = Depends(require_admin)):
        return {"kind": principal.kind, "tenant_ids": principal.tenant_ids}

    @app.get("/tenants/{tenant_id}/thing")
    async def thing(principal: AdminPrincipal = Depends(require_tenant_access)):
        return {"kind": principal.kind}

    return app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TOKEN)
    reset_settings_cache()
    with TestClient(_app()) as test_client:
        yield test_client
    reset_settings_cache()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_no_token_configured_is_401_everywhere():
    """The deliberate break with fail-open: every other guard in
    app/channels/security.py treats an unset secret as "stay open for dev
    convenience". This one doesn't — an unauthenticated admin request
    rewrites every tenant's config and reads every transcript."""
    assert get_settings().admin_auth_token is None
    with TestClient(_app()) as anon_client:
        response = anon_client.get("/whoami")
    assert response.status_code == 401


def test_valid_bearer_returns_the_operator_principal(client):
    response = client.get("/whoami", headers=_bearer(_TOKEN))
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "operator"
    assert body["tenant_ids"] is None


def test_missing_bearer_is_401(client):
    assert client.get("/whoami").status_code == 401


def test_wrong_bearer_is_401(client):
    assert client.get("/whoami", headers=_bearer("wrong")).status_code == 401


def test_api_auth_token_is_rejected(client, monkeypatch):
    """The privilege-separation regression guard: API_AUTH_TOKEN is a
    different secret with different power ("run a conversation as any
    tenant") — presenting it here must never work, even by coincidence."""
    monkeypatch.setenv("API_AUTH_TOKEN", "chat-secret-token")
    reset_settings_cache()
    response = client.get("/whoami", headers=_bearer("chat-secret-token"))
    assert response.status_code == 401


def test_operator_reaches_any_tenant(client):
    response = client.get("/tenants/hotel-mzv/thing", headers=_bearer(_TOKEN))
    assert response.status_code == 200
    response = client.get("/tenants/northside-plumbing/thing", headers=_bearer(_TOKEN))
    assert response.status_code == 200


def test_tenant_principal_is_scoped_to_its_own_tenants():
    """No real tenant-login branch exists yet — this exercises the future
    path today by constructing a scoped AdminPrincipal directly, exactly as
    plans/phase8.md's "designed for tenant login later" contract calls for."""
    scoped = AdminPrincipal(kind="tenant", tenant_ids=("hotel-mzv",), subject="user_123")
    assert scoped.may_access("hotel-mzv") is True
    assert scoped.may_access("northside-plumbing") is False
    assert scoped.may_write("hotel-mzv") is True
    assert scoped.may_write("northside-plumbing") is False


def test_operator_principal_may_access_and_write_everywhere():
    operator = AdminPrincipal(kind="operator", tenant_ids=None, subject="operator")
    assert operator.may_access("anything-at-all")
    assert operator.may_write("anything-at-all")


def test_failed_auth_throttle_429s_after_the_limit(client):
    for _ in range(admin_auth._FAILED_AUTH_LIMIT):
        response = client.get("/whoami", headers=_bearer("wrong"))
        assert response.status_code == 401

    throttled = client.get("/whoami", headers=_bearer("wrong"))
    assert throttled.status_code == 429
    assert "Retry-After" in throttled.headers


def test_successful_auth_never_counts_against_the_failure_throttle(client):
    """A legitimate high-frequency operator must never trip the failed-auth
    throttle via their own valid calls — only failures count against it."""
    for _ in range(admin_auth._FAILED_AUTH_LIMIT + 5):
        response = client.get("/whoami", headers=_bearer(_TOKEN))
        assert response.status_code == 200
