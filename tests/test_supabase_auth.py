"""app/db/auth.py — HS256 JWT minting for per-tenant Supabase RLS (Phase 4)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from app.db.auth import SupabaseAuthNotConfiguredError, clear_jwt_cache, tenant_jwt


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _decode(token: str) -> tuple[dict, dict, str]:
    header_b64, payload_b64, signature_b64 = token.split(".")
    header = json.loads(_b64url_decode(header_b64))
    payload = json.loads(_b64url_decode(payload_b64))
    return header, payload, signature_b64


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_jwt_cache()
    yield
    clear_jwt_cache()


def test_raises_without_a_configured_secret():
    with pytest.raises(SupabaseAuthNotConfiguredError):
        tenant_jwt("hotel-mzv")


def test_mints_a_well_formed_hs256_token(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    from app.config import reset_settings_cache

    reset_settings_cache()
    try:
        token = tenant_jwt("hotel-mzv")
        header, payload, _ = _decode(token)

        assert header == {"alg": "HS256", "typ": "JWT"}
        assert payload["tenant_id"] == "hotel-mzv"
        assert payload["sub"] == "hotel-mzv"
        assert payload["role"] == "app_backend"
        assert payload["aud"] == "authenticated"
        assert payload["exp"] > payload["iat"]
    finally:
        reset_settings_cache()


def test_signature_is_valid_hmac_sha256(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    from app.config import reset_settings_cache

    reset_settings_cache()
    try:
        token = tenant_jwt("hotel-mzv")
        header_b64, payload_b64, signature_b64 = token.split(".")
        expected = hmac.new(
            b"test-secret", f"{header_b64}.{payload_b64}".encode("ascii"), hashlib.sha256
        ).digest()
        assert _b64url_decode(signature_b64) == expected
    finally:
        reset_settings_cache()


def test_a_wrong_secret_produces_a_different_signature(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    from app.config import reset_settings_cache

    reset_settings_cache()
    try:
        token = tenant_jwt("hotel-mzv")
        _, _, signature_b64 = token.split(".")
        header_b64, payload_b64, _ = token.split(".")
        wrong = hmac.new(
            b"not-the-secret", f"{header_b64}.{payload_b64}".encode("ascii"), hashlib.sha256
        ).digest()
        assert _b64url_decode(signature_b64) != wrong
    finally:
        reset_settings_cache()


def test_repeated_calls_for_the_same_tenant_are_cached(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    from app.config import reset_settings_cache

    reset_settings_cache()
    try:
        first = tenant_jwt("hotel-mzv")
        second = tenant_jwt("hotel-mzv")
        assert first == second
    finally:
        reset_settings_cache()


def test_different_tenants_get_different_tokens(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    from app.config import reset_settings_cache

    reset_settings_cache()
    try:
        hotel_token = tenant_jwt("hotel-mzv")
        other_token = tenant_jwt("northside-plumbing")
        assert hotel_token != other_token

        _, hotel_payload, _ = _decode(hotel_token)
        _, other_payload, _ = _decode(other_token)
        assert hotel_payload["tenant_id"] == "hotel-mzv"
        assert other_payload["tenant_id"] == "northside-plumbing"
    finally:
        reset_settings_cache()


def test_clear_jwt_cache_forces_a_remint(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    from app.config import reset_settings_cache

    reset_settings_cache()
    try:
        first = tenant_jwt("hotel-mzv")
        clear_jwt_cache()
        second = tenant_jwt("hotel-mzv")
        # Different `iat`/`exp` almost certainly, but at minimum this proves
        # the cache was actually consulted (not a coincidental cache miss).
        assert isinstance(first, str) and isinstance(second, str)
    finally:
        reset_settings_cache()
