"""The one service: health + the chat channel adapter."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings, reset_settings_cache
from app.db.memory_store import get_store
from app.main import app
from tests.conftest import ai


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _sse_payloads(body: str) -> list[dict]:
    events = []
    for line in body.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[6:]))
    return events


def test_health_lists_configured_tenants(client):
    response = client.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert "hotel-mzv" in body["tenants"]
    assert body["llm_provider"] == get_settings().llm_provider


def test_health_reports_memory_store_by_default(client):
    """No SUPABASE_URL configured -> the in-memory store is active (Phase 4)."""
    assert get_settings().supabase_url is None
    body = client.get("/health").json()
    assert body["store"] == "memory"


def test_health_reports_supabase_store_when_configured(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    reset_settings_cache()
    try:
        body = client.get("/health").json()
        assert body["store"] == "supabase"
    finally:
        reset_settings_cache()


def test_supabase_fields_are_real_settings_fields():
    """Guards the Phase 4 lesson from plan §1: `hermetic_settings` only strips env
    vars matching a `Settings` field name. A Supabase var read via `os.environ`
    directly (instead of through `Settings`) would leak the developer's box into
    the suite the moment it's exported — this pins them as real fields so that
    can't happen silently."""
    fields = get_settings().model_fields
    for name in (
        "supabase_url",
        "supabase_anon_key",
        "supabase_secret_key",
        "supabase_jwt_secret",
        "supabase_timeout_seconds",
        "secret_cache_ttl_seconds",
        "tenant_source",
        "database_url",
        "checkpoint_retention_hours",
    ):
        assert name in fields, f"{name} must be a real Settings field, not an ad-hoc env read"


def test_chat_streams_sse_events(client, scripted, hotel):
    scripted(ai("Hi there, how can I help?"))

    response = client.post(
        "/chat", json={"message": "hello", "tenant_id": hotel.tenant_id, "session_id": "api"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.rstrip().endswith("data: [DONE]")

    payloads = _sse_payloads(response.text)
    assert len(payloads) > 3  # streamed, not one blob
    assert payloads[-1]["type"] == "final"
    assert "how can I help" in payloads[-1]["text"]


def test_chat_routes_by_widget_key_without_trusting_a_client_tenant_id(client, scripted):
    model = scripted(ai("Northside Plumbing, what's up?"))

    response = client.post(
        "/chat",
        json={"message": "hello", "widget_key": "pk_widget_northside_demo", "session_id": "w"},
    )

    assert response.status_code == 200
    system_prompt = str(model.seen_prompts[0][0].content)
    assert "Northside Plumbing" in system_prompt


def test_chat_can_book_through_the_http_channel(client, scripted, hotel):
    """Same graph, same tools — a chat booking is a real booking (plan §8)."""
    scripted(
        ai("Let me look. ", [{"name": "check_availability", "args": {"service": "diagnostic"}}]),
        ai("How about the first one?"),
    )

    response = client.post(
        "/chat",
        json={"message": "my lights are out", "tenant_id": hotel.tenant_id, "session_id": "book"},
    )

    # tool_start/tool_result are deliberately filtered from the public stream
    # (Phase 5) — the turn still completes normally with the tool having run.
    payloads = _sse_payloads(response.text)
    assert not any(p["type"] in ("tool_start", "tool_result") for p in payloads)
    assert payloads[-1]["type"] == "final"
    assert "first one" in payloads[-1]["text"]


def test_chat_rejects_an_empty_message(client):
    assert client.post("/chat", json={"message": ""}).status_code == 422


def test_chat_requires_the_bearer_token_when_one_is_configured(
    client, scripted, hotel, monkeypatch
):
    monkeypatch.setenv("API_AUTH_TOKEN", "s3cret")
    reset_settings_cache()
    try:
        scripted(ai("hello"))
        payload = {"message": "hi", "tenant_id": hotel.tenant_id, "session_id": "auth"}

        assert client.post("/chat", json=payload).status_code == 401
        assert (
            client.post(
                "/chat", json=payload, headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/chat", json=payload, headers={"Authorization": "Bearer s3cret"}
            ).status_code
            == 200
        )
    finally:
        monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
        reset_settings_cache()


def test_voice_endpoints_are_live(client):
    """Phase 2 replaced the 501 stubs. Detail is in tests/test_vapi_*.py."""
    assert client.post("/chat/completions", content=b"not json").status_code == 400
    assert client.post("/webhooks/vapi", json={"message": {"type": "status-update"}}).json() == {
        "status": "ignored",
        "type": "status-update",
    }


def test_store_is_empty_between_tests():
    assert get_store().list_jobs("hotel-mzv") == []


def test_widget_bundle_missing_is_a_clean_404(client, monkeypatch, tmp_path):
    import app.main as main_module

    monkeypatch.setattr(main_module, "WIDGET_BUNDLE_PATH", tmp_path / "does-not-exist.js")
    response = client.get("/widget.js")
    assert response.status_code == 404


def test_widget_bundle_is_served_when_built(client):
    """Phase 5 Step 7 committed widget/dist/widget.js — this hits the real
    file on disk, same as tests/test_widget_bundle.py's hash guard."""
    response = client.get("/widget.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.headers["cache-control"].startswith("public")


def test_widget_demo_page_is_served(client):
    response = client.get("/widget/demo")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "widget.js" in response.text


def test_health_reports_widget_built(client):
    assert client.get("/health").json()["widget"] == "built"


def test_health_reports_widget_missing_when_the_bundle_is_absent(client, monkeypatch, tmp_path):
    import app.main as main_module

    monkeypatch.setattr(main_module, "WIDGET_BUNDLE_PATH", tmp_path / "does-not-exist.js")
    body = client.get("/health").json()
    assert body["widget"] == "missing"
    assert body["status"] == "degraded"
    assert "widget bundle not built" in body["problems"]


def test_health_is_ok_with_no_problems_by_default(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["problems"] == []


def test_health_detail_is_open_when_no_api_auth_token_is_configured(client):
    """Dev default: no API_AUTH_TOKEN means every caller sees the detail
    (Phase 7 Step 4) — same fail-open-when-unconfigured convention as
    app/channels/security.py."""
    assert get_settings().api_auth_token is None
    body = client.get("/health").json()
    assert "tenants" in body
    assert "llm_provider" in body


def test_health_detail_requires_the_bearer_once_a_token_is_configured(client, monkeypatch):
    monkeypatch.setenv("API_AUTH_TOKEN", "s3cret")
    reset_settings_cache()
    try:
        anonymous = client.get("/health").json()
        assert "tenants" not in anonymous
        assert "llm_provider" not in anonymous
        assert "env" not in anonymous
        assert "model" not in anonymous
        # Still 200 and still reports the operational fields -- a health
        # checker with no Authorization header must never see a failure.
        assert anonymous["status"] == "ok"

        authed = client.get("/health", headers={"Authorization": "Bearer s3cret"}).json()
        assert "hotel-mzv" in authed["tenants"]
    finally:
        reset_settings_cache()


def test_readyz_reports_ready_on_the_memory_store(client):
    assert get_settings().supabase_url is None
    body = client.get("/readyz").json()
    assert body == {"ready": True, "store": "memory"}


def test_readyz_503s_when_supabase_is_unreachable(client, monkeypatch):
    from app.db.factory import reset_store_cache
    from app.db.supabase_store import SupabaseStore, SupabaseStoreError

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    reset_settings_cache()

    async def _boom(self, tenant_id, **kwargs):
        raise SupabaseStoreError("boom")

    monkeypatch.setattr(SupabaseStore, "alist_jobs", _boom)
    try:
        response = client.get("/readyz")
        assert response.status_code == 503
    finally:
        reset_settings_cache()
        reset_store_cache()


def test_cors_preflight_allows_a_cross_origin_chat_call(client):
    response = client.options(
        "/chat",
        headers={
            "Origin": "https://client-site.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


# --- serving the admin UI (Phase 8) -----------------------------------------


def test_health_reports_admin_missing_when_enabled_but_not_built(client, monkeypatch, tmp_path):
    import app.main as main_module

    monkeypatch.setenv("ADMIN_ENABLED", "true")
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "a" * 40)
    reset_settings_cache()
    monkeypatch.setattr(main_module, "ADMIN_INDEX_PATH", tmp_path / "does-not-exist.html")
    try:
        body = client.get("/health").json()
        assert body["admin"] == "missing"
        assert body["status"] == "degraded"
        assert "admin bundle not built" in body["problems"]
    finally:
        reset_settings_cache()


def test_health_admin_not_reported_as_a_problem_when_admin_is_disabled(
    client, monkeypatch, tmp_path
):
    """A box that never opted into ADMIN_ENABLED shouldn't get a spurious
    "problem" for a bundle it was never asked to build."""
    import app.main as main_module

    monkeypatch.setattr(main_module, "ADMIN_INDEX_PATH", tmp_path / "does-not-exist.html")
    body = client.get("/health").json()
    assert body["admin"] == "missing"
    assert body["status"] == "ok"
    assert body["problems"] == []


def test_route_ordering_admin_api_is_never_shadowed_by_the_spa_catch_all(client, monkeypatch):
    """The risk this exists to close: a catch-all registered before the API
    router would return this page's HTML to a JSON client. /admin/api/tenants
    structurally matches the wildcard route too (`{path:path}` captures
    slashes) — this proves registration order wins, not just that the route
    "happens to" return JSON today."""
    monkeypatch.setenv("ADMIN_ENABLED", "true")
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "a" * 40)
    reset_settings_cache()
    try:
        response = client.get("/admin/api/tenants", headers={"Authorization": f"Bearer {'a' * 40}"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
    finally:
        reset_settings_cache()


def test_admin_spa_catch_all_serves_the_built_index_for_any_deep_link(client):
    """The router lives client-side in the URL hash, so the server renders
    the identical index.html for every path — a deep link like
    /admin/tenants/hotel-mzv (or anything else) must not 404."""
    response = client.get("/admin/tenants/hotel-mzv/config")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_admin_spa_catch_all_404s_with_a_pointer_when_unbuilt(client, monkeypatch, tmp_path):
    import app.main as main_module

    monkeypatch.setattr(main_module, "ADMIN_INDEX_PATH", tmp_path / "does-not-exist.html")
    response = client.get("/admin/anything")
    assert response.status_code == 404
    assert "npm --prefix admin run build" in response.json()["detail"]
