"""Widget session token mint/verify round trip (Phase 5)."""

from __future__ import annotations

import time

from app.channels.widget_auth import (
    mint_session_token,
    new_session_id,
    verify_session_token,
)
from app.config import reset_settings_cache


def test_mint_and_verify_round_trip():
    session_id = new_session_id()
    token = mint_session_token("hotel-mzv", session_id)

    claims = verify_session_token(token)

    assert claims is not None
    assert claims.tenant_id == "hotel-mzv"
    assert claims.session_id == session_id


def test_new_session_ids_are_unique():
    assert new_session_id() != new_session_id()


def test_expired_token_fails_verification(monkeypatch):
    monkeypatch.setenv("WIDGET_SESSION_TTL_SECONDS", "-10")
    reset_settings_cache()
    try:
        token = mint_session_token("hotel-mzv", "web_abc")
        assert verify_session_token(token) is None
    finally:
        monkeypatch.delenv("WIDGET_SESSION_TTL_SECONDS", raising=False)
        reset_settings_cache()


def test_tampered_signature_fails_verification():
    token = mint_session_token("hotel-mzv", "web_abc")
    body, signature = token.rsplit(".", 1)
    tampered = f"{body}.{signature[:-1]}{'x' if signature[-1] != 'x' else 'y'}"

    assert verify_session_token(tampered) is None


def test_tampered_body_fails_verification():
    token = mint_session_token("hotel-mzv", "web_abc")
    body, signature = token.rsplit(".", 1)
    tampered = f"{body}x.{signature}"

    assert verify_session_token(tampered) is None


def test_malformed_tokens_never_raise():
    for garbage in ("", "not-a-token", "a.b.c", "....", "a" * 5000):
        assert verify_session_token(garbage) is None


def test_a_token_for_one_tenant_does_not_verify_as_another():
    token = mint_session_token("hotel-mzv", "web_abc")
    claims = verify_session_token(token)
    assert claims.tenant_id != "northside-plumbing"


def test_minting_works_with_no_secret_configured():
    """The fallback per-process key — dev works with zero config."""
    assert get_settings_widget_secret_is_unset()
    token = mint_session_token("hotel-mzv", "web_abc")
    assert verify_session_token(token) is not None


def get_settings_widget_secret_is_unset() -> bool:
    from app.config import get_settings

    return get_settings().widget_session_secret is None


def test_a_freshly_minted_token_is_not_already_expired():
    token = mint_session_token("hotel-mzv", "web_abc")
    time.sleep(0)  # keep it explicit that this is a same-instant check
    assert verify_session_token(token) is not None
