"""Phase 7 Step 3 — production preflight never blocks dev/staging, and names
every missing secret rather than failing on the first one."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.preflight import verify_production_settings

_COMPLETE = dict(
    app_env="production",
    api_auth_token="tok",
    vapi_webhook_secret="sh4red",
    widget_session_secret="widgetsecret",
    public_base_url="https://api.example.com",
    llm_provider="groq",
    groq_api_key="gsk_test",
)


def test_a_fully_configured_production_settings_has_no_problems():
    assert verify_production_settings(Settings(**_COMPLETE)) == []


@pytest.mark.parametrize("env", ["development", "staging"])
def test_non_production_never_fails_regardless_of_missing_secrets(env):
    settings = Settings(app_env=env)
    assert verify_production_settings(settings) == []


@pytest.mark.parametrize(
    "missing_key",
    ["api_auth_token", "vapi_webhook_secret", "widget_session_secret", "public_base_url"],
)
def test_each_missing_secret_fails_production_alone(missing_key):
    config = dict(_COMPLETE)
    config[missing_key] = None
    problems = verify_production_settings(Settings(**config))
    assert len(problems) == 1
    assert missing_key.upper() in problems[0]


def test_public_base_url_must_be_https():
    config = dict(_COMPLETE, public_base_url="http://api.example.com")
    problems = verify_production_settings(Settings(**config))
    assert len(problems) == 1
    assert "https" in problems[0]


def test_supabase_url_without_jwt_secret_fails():
    config = dict(_COMPLETE, supabase_url="https://project.supabase.co")
    problems = verify_production_settings(Settings(**config))
    assert len(problems) == 1
    assert "SUPABASE_JWT_SECRET" in problems[0]


def test_supabase_url_with_jwt_secret_is_clean():
    config = dict(
        _COMPLETE,
        supabase_url="https://project.supabase.co",
        supabase_jwt_secret="jwtsecret",
    )
    assert verify_production_settings(Settings(**config)) == []


def test_active_llm_provider_needs_its_own_api_key():
    config = dict(_COMPLETE, llm_provider="google", google_api_key=None)
    problems = verify_production_settings(Settings(**config))
    assert len(problems) == 1
    assert "GOOGLE_API_KEY" in problems[0]


def test_every_problem_is_named_at_once_not_just_the_first():
    problems = verify_production_settings(Settings(app_env="production"))
    assert len(problems) >= 5
