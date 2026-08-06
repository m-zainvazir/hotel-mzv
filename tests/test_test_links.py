"""app/channels/test_links.py — signing/verification, and the two `/test/...`
routes in app/main.py (Phase 9.1).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.channels.test_links import mint_test_token, verify_test_token
from app.channels.widget_auth import mint_session_token, verify_session_token
from app.config import reset_settings_cache
from app.main import app
from app.tenancy import loader
from app.tenancy.repository import TenantNotFoundError


class _StaticRepo:
    """A repository serving exactly one tenant on every lookup path — mirrors
    test_admin_tenant_crud.py's own fixture of the same name; kept as a
    local copy rather than a cross-test-module import, matching this
    codebase's convention of each test file being self-contained."""

    def __init__(self, tenant) -> None:
        self.tenant = tenant

    def get(self, tenant_id: str):
        if tenant_id == self.tenant.tenant_id:
            return self.tenant
        raise TenantNotFoundError(tenant_id)

    def list_ids(self) -> list[str]:
        return [self.tenant.tenant_id]

    def find_by_phone(self, phone_number: str):
        return self.tenant if phone_number in self.tenant.phone_numbers else None

    def find_by_widget_key(self, widget_key: str):
        return self.tenant if widget_key in self.tenant.widget_keys else None

    def find_by_assistant_id(self, assistant_id: str):
        return None


class TestMintVerifyRoundTrip:
    def test_a_freshly_minted_token_verifies(self, hotel):
        token = mint_test_token(hotel.tenant_id)
        claims = verify_test_token(token)
        assert claims is not None
        assert claims.tenant_id == hotel.tenant_id
        assert claims.mode == "chat"
        assert claims.variant == "live"

    def test_voice_mode_can_be_minted_and_verified(self, hotel):
        token = mint_test_token(hotel.tenant_id, mode="voice")
        claims = verify_test_token(token)
        assert claims is not None
        assert claims.mode == "voice"

    def test_garbage_input_fails_closed(self):
        assert verify_test_token("not-a-real-token") is None
        assert verify_test_token("") is None
        assert verify_test_token("a.b.c") is None

    def test_a_tampered_body_is_rejected(self, hotel):
        token = mint_test_token(hotel.tenant_id)
        body, sig = token.split(".", 1)
        tampered = f"{body}x.{sig}"
        assert verify_test_token(tampered) is None

    def test_a_tampered_signature_is_rejected(self, hotel):
        token = mint_test_token(hotel.tenant_id)
        body, sig = token.split(".", 1)
        tampered = f"{body}.{sig[:-1]}{'a' if sig[-1] != 'a' else 'b'}"
        assert verify_test_token(tampered) is None

    def test_an_expired_token_is_rejected(self, hotel):
        token = mint_test_token(hotel.tenant_id, ttl_seconds=-1)
        assert verify_test_token(token) is None

    def test_a_custom_ttl_is_honoured(self, hotel):
        # Comfortably expired vs. comfortably alive — avoids a real-time
        # sleep racing against second-granularity truncation in the claim.
        assert verify_test_token(mint_test_token(hotel.tenant_id, ttl_seconds=-100)) is None
        assert verify_test_token(mint_test_token(hotel.tenant_id, ttl_seconds=100)) is not None


class TestSecretSeparationFromWidgetSessions:
    """A leaked test link must never be replayable as a chat session token,
    and vice versa — the two signers use entirely different claim shapes
    even when they fall back to the same secret."""

    def test_a_widget_session_token_is_rejected_by_verify_test_token(self, hotel):
        token = mint_session_token(hotel.tenant_id, "sess_123")
        assert verify_test_token(token) is None

    def test_a_test_link_token_is_rejected_by_verify_session_token(self, hotel):
        token = mint_test_token(hotel.tenant_id)
        assert verify_session_token(token) is None

    def test_a_dedicated_secret_is_used_when_configured(self, hotel, monkeypatch):
        monkeypatch.setenv("TEST_LINK_SECRET", "a-dedicated-test-link-secret")
        monkeypatch.setenv("WIDGET_SESSION_SECRET", "a-different-widget-secret")
        reset_settings_cache()
        try:
            token = mint_test_token(hotel.tenant_id)
            assert verify_test_token(token) is not None
        finally:
            reset_settings_cache()


# --- app/main.py's /test/{token} and /test/session --------------------------


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _public_base_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    reset_settings_cache()
    yield
    reset_settings_cache()


class TestTestAgentPage:
    def test_a_valid_token_serves_the_widget_embedded_with_the_test_token(self, client, hotel):
        token = mint_test_token(hotel.tenant_id)
        response = client.get(f"/test/{token}")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert f'data-test-token="{token}"' in response.text
        assert "data-auto-open" in response.text
        assert "/widget.js" in response.text

    def test_an_invalid_token_is_404(self, client):
        response = client.get("/test/not-a-real-token")
        assert response.status_code == 404

    def test_an_expired_token_is_404(self, client, hotel):
        token = mint_test_token(hotel.tenant_id, ttl_seconds=-1)
        response = client.get(f"/test/{token}")
        assert response.status_code == 404

    def test_a_voice_mode_token_is_refused_until_the_voice_tester_ships(self, client, hotel):
        token = mint_test_token(hotel.tenant_id, mode="voice")
        response = client.get(f"/test/{token}")
        assert response.status_code == 404
        assert "9.3" in response.json()["detail"]

    def test_unknown_tenant_is_404(self, client):
        # A token was minted once for a real tenant, then that tenant
        # vanished from the repository underneath it.
        token = mint_test_token("some-tenant-that-does-not-exist")
        response = client.get(f"/test/{token}")
        assert response.status_code == 404


class TestTestSession:
    def test_a_valid_token_starts_a_real_chat_session(self, client, hotel):
        token = mint_test_token(hotel.tenant_id)
        response = client.post("/test/session", json={"token": token})
        assert response.status_code == 200
        body = response.json()
        assert body["session_id"]
        assert body["token"]
        assert body["tenant"]["name"] == hotel.name

    def test_no_widget_key_needed(self, client, hotel):
        """A tenant with an empty widget_keys[] is still testable — the
        session doesn't come from a widget key at all here."""
        tenant = hotel.model_copy(update={"widget_keys": []})
        original = loader._repository
        loader.set_repository(_StaticRepo(tenant))
        try:
            token = mint_test_token(tenant.tenant_id)
            response = client.post("/test/session", json={"token": token})
        finally:
            loader.set_repository(original)
        assert response.status_code == 200

    def test_an_invalid_token_is_404(self, client):
        response = client.post("/test/session", json={"token": "garbage"})
        assert response.status_code == 404

    def test_a_voice_mode_token_is_refused(self, client, hotel):
        token = mint_test_token(hotel.tenant_id, mode="voice")
        response = client.post("/test/session", json={"token": token})
        assert response.status_code == 404

    def test_the_minted_session_token_is_a_real_widget_session_token(self, client, hotel):
        """The whole point of reusing app/channels/chat.py's start_session:
        the resulting token must work exactly like a normal widget session
        token on POST /chat."""
        token = mint_test_token(hotel.tenant_id)
        session = client.post("/test/session", json={"token": token}).json()
        claims = verify_session_token(session["token"])
        assert claims is not None
        assert claims.tenant_id == hotel.tenant_id
        assert claims.session_id == session["session_id"]


class TestDraftVariantResolution:
    """`/test/{token}` and `/test/session` with `variant="draft"` — the
    DISPLAY half only (greeting/services shown before the first message);
    tests/test_draft_preview.py covers the actual conversation."""

    def test_no_draft_falls_back_to_live_display(self, client, hotel, monkeypatch):
        async def _no_draft(tenant_id, *, client=None):
            return None, None

        monkeypatch.setattr("app.main.get_draft", _no_draft)

        token = mint_test_token(hotel.tenant_id, variant="draft")
        response = client.post("/test/session", json={"token": token})
        assert response.status_code == 200
        assert response.json()["tenant"]["greeting"] == (hotel.chat.greeting or hotel.greeting)

    def test_a_draft_greeting_is_shown_in_draft_mode(self, client, hotel, monkeypatch):
        draft = hotel.model_copy(update={"greeting": "UNMISTAKABLE DRAFT GREETING"})

        async def _fake_draft(tenant_id, *, client=None):
            return draft, "d1"

        monkeypatch.setattr("app.main.get_draft", _fake_draft)

        token = mint_test_token(hotel.tenant_id, variant="draft")
        response = client.post("/test/session", json={"token": token})
        assert response.status_code == 200
        assert response.json()["tenant"]["greeting"] == "UNMISTAKABLE DRAFT GREETING"

    def test_live_variant_never_reads_the_draft_at_all(self, client, hotel, monkeypatch):
        called = {"n": 0}

        async def _spy_draft(tenant_id, *, client=None):
            called["n"] += 1
            return None, None

        monkeypatch.setattr("app.main.get_draft", _spy_draft)

        token = mint_test_token(hotel.tenant_id, variant="live")
        response = client.post("/test/session", json={"token": token})
        assert response.status_code == 200
        assert called["n"] == 0

    def test_the_minted_session_token_itself_carries_the_draft_variant(
        self, client, hotel, monkeypatch
    ):
        """The actual mechanism that makes the real conversation use the
        draft: the resulting widget session token's own claims, not
        anything about this one handshake response."""

        async def _no_draft(tenant_id, *, client=None):
            return None, None

        monkeypatch.setattr("app.main.get_draft", _no_draft)

        token = mint_test_token(hotel.tenant_id, variant="draft")
        session = client.post("/test/session", json={"token": token}).json()
        claims = verify_session_token(session["token"])
        assert claims is not None
        assert claims.variant == "draft"

    def test_draft_mode_page_shows_a_draft_banner(self, client, hotel, monkeypatch):
        async def _no_draft(tenant_id, *, client=None):
            return None, None

        monkeypatch.setattr("app.main.get_draft", _no_draft)

        token = mint_test_token(hotel.tenant_id, variant="draft")
        response = client.get(f"/test/{token}")
        assert response.status_code == 200
        assert "Draft preview" in response.text

    def test_live_mode_page_has_no_draft_banner(self, client, hotel):
        token = mint_test_token(hotel.tenant_id, variant="live")
        response = client.get(f"/test/{token}")
        assert response.status_code == 200
        assert "Draft preview" not in response.text


class TestChannelDisabledRefusesTestSurface:
    def test_chat_disabled_tenant_refuses_test_agent_page(self, client, hotel):
        disabled = hotel.model_copy(
            update={
                "channels": hotel.channels.model_copy(
                    update={"chat": hotel.channels.chat.model_copy(update={"enabled": False})}
                )
            }
        )
        original = loader._repository
        loader.set_repository(_StaticRepo(disabled))
        try:
            token = mint_test_token(disabled.tenant_id)
            response = client.get(f"/test/{token}")
        finally:
            loader.set_repository(original)
        assert response.status_code == 404

    def test_chat_disabled_tenant_refuses_test_session(self, client, hotel):
        disabled = hotel.model_copy(
            update={
                "channels": hotel.channels.model_copy(
                    update={"chat": hotel.channels.chat.model_copy(update={"enabled": False})}
                )
            }
        )
        original = loader._repository
        loader.set_repository(_StaticRepo(disabled))
        try:
            token = mint_test_token(disabled.tenant_id)
            response = client.post("/test/session", json={"token": token})
        finally:
            loader.set_repository(original)
        assert response.status_code == 404


def test_the_test_agent_page_cache_busts_the_widget_bundle(client, hotel):
    """A stale bundle on the Test Agent page is indistinguishable from a
    broken feature — `/widget.js` shipped `Cache-Control: immutable`, so a
    browser that had fetched it once stopped asking, and a page load made no
    request at all while running a months-old bundle. A real embed can't
    carry a query (the bare tag is the frozen contract), but this page is
    server-rendered every load, so it can."""
    from app.main import WIDGET_BUILDHASH_PATH

    token = mint_test_token(hotel.tenant_id)
    body = client.get(f"/test/{token}").text
    assert 'src="/widget.js?v=' in body
    assert 'src="/widget.js"' not in body
    expected = WIDGET_BUILDHASH_PATH.read_text(encoding="utf-8").strip()[:12]
    assert f"?v={expected}" in body


class TestSharedBotPage:
    """`GET /bot/{widget_key}` — the permanent public "Share agent" link.

    Deliberately not a Test Agent link: those are signed, private and
    expire, which is right for previewing a draft and wrong for a URL a
    client puts in an email. This one is addressed by the tenant's own
    public widget key, never expires, and always serves LIVE config.
    """

    def test_a_valid_widget_key_serves_the_widget(self, client, hotel):
        key = hotel.widget_keys[0]
        response = client.get(f"/bot/{key}")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert f'data-widget-key="{key}"' in response.text
        # The frozen embed contract, not a test token — a share link must
        # keep working when no operator is around to re-mint anything.
        assert "data-test-token" not in response.text
        assert hotel.name in response.text

    def test_an_unknown_widget_key_is_404(self, client):
        assert client.get("/bot/pk_widget_not_a_real_key").status_code == 404

    def test_it_is_cache_busted_like_the_test_page(self, client, hotel):
        assert 'src="/widget.js?v=' in client.get(f"/bot/{hotel.widget_keys[0]}").text

    def test_a_chat_disabled_tenant_is_404(self, client, hotel, override_tenant):
        from app.tenancy.models import ChannelToggle

        # `model_copy` doesn't validate, so the nested value has to be a real
        # model — a bare dict silently stays a dict and fails at read time.
        tenant = hotel.model_copy(
            update={
                "channels": hotel.channels.model_copy(update={"chat": ChannelToggle(enabled=False)})
            }
        )
        override_tenant(tenant)
        assert client.get(f"/bot/{tenant.widget_keys[0]}").status_code == 404


class TestOwnOriginIsAllowed:
    """A tenant that restricts `allowed_origins` to its own website would
    otherwise find its hosted share link 403ing — the page is on OUR origin,
    which is the one origin the restriction was never meant to exclude."""

    def test_our_own_hosted_page_is_not_blocked_by_allowed_origins(
        self, client, hotel, override_tenant, monkeypatch
    ):
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
        reset_settings_cache()
        tenant = hotel.model_copy(
            update={
                "chat": hotel.chat.model_copy(
                    update={"allowed_origins": ["https://a-client.example"]}
                )
            }
        )
        override_tenant(tenant)
        response = client.post(
            "/chat/session",
            json={"widget_key": tenant.widget_keys[0]},
            headers={"Origin": "https://example.test"},
        )
        assert response.status_code == 200

    def test_a_third_party_origin_is_still_blocked(self, client, hotel, override_tenant):
        tenant = hotel.model_copy(
            update={
                "chat": hotel.chat.model_copy(
                    update={"allowed_origins": ["https://a-client.example"]}
                )
            }
        )
        override_tenant(tenant)
        response = client.post(
            "/chat/session",
            json={"widget_key": tenant.widget_keys[0]},
            headers={"Origin": "https://somewhere-else.example"},
        )
        assert response.status_code == 403
