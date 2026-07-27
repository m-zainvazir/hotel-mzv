"""Phase 7 Step 7 — LangSmith tracing is now real Settings fields exported
into os.environ by `lifespan`, not silently-inert `.env` entries.

The bug: `LANGCHAIN_TRACING_V2` et al. were always in `.env.example`, but
were never real `Settings` fields (`extra="ignore"` swallowed them) and
nothing called `load_dotenv()` — so pydantic-settings read `.env` without
exporting to `os.environ`, and LangChain's tracer reads `os.environ`. The
documented switch did nothing under uvicorn.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings, reset_settings_cache
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_langsmith_fields_are_real_settings_fields():
    """A field read straight from os.environ (instead of through Settings)
    would leak the developer's box into the suite the moment it's exported,
    and would never be stripped by hermetic_settings -- see the Phase 4
    lesson this mirrors (test_supabase_fields_are_real_settings_fields)."""
    fields = get_settings().model_fields
    for name in ("langchain_tracing_v2", "langchain_api_key", "langchain_project"):
        assert name in fields, f"{name} must be a real Settings field, not an ad-hoc env read"


def test_tracing_is_off_by_default():
    settings = get_settings()
    assert settings.langchain_tracing_v2 is False
    assert settings.langchain_api_key is None


def test_health_reports_tracing_false_when_unconfigured(client):
    body = client.get("/health").json()
    assert body["tracing"] is False


def test_health_reports_tracing_true_only_when_both_flag_and_key_are_set(client, monkeypatch):
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    reset_settings_cache()
    try:
        # Flag alone, no key -> still off. Tracing needs somewhere to send runs.
        assert client.get("/health").json()["tracing"] is False

        monkeypatch.setenv("LANGCHAIN_API_KEY", "ls__test")
        reset_settings_cache()
        assert client.get("/health").json()["tracing"] is True
    finally:
        reset_settings_cache()
