"""app/tenancy/voice.py — Cartesia cloning gated on recorded consent (Phase 4).

No network — every client is httpx.MockTransport via mock_http().
"""

from __future__ import annotations

import httpx
import pytest

from app.config import reset_settings_cache
from app.tenancy.voice import (
    CartesiaError,
    VoiceConsentError,
    clone_voice,
    has_consent,
    record_consent,
)
from tests.conftest import mock_http


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("CARTESIA_API_KEY", "sk_car_test")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "sample.wav"
    path.write_bytes(b"not-real-audio-just-test-bytes")
    return str(path)


class TestRecordConsent:
    async def test_request_shape(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json=[{"id": 1, "tenant_id": "hotel-mzv"}])

        client, requests = mock_http(handler)
        result = await record_consent(
            tenant_id="hotel-mzv",
            voice_owner_name="Zain",
            consent_artifact_url="https://example.com/consent.pdf",
            granted_by="zain@example.com",
            client=client,
        )

        assert requests[0].url.path == "/voice_consents"
        assert requests[0].headers["prefer"] == "return=representation"
        assert result["tenant_id"] == "hotel-mzv"

    async def test_missing_supabase_config_raises_without_http_call(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        reset_settings_cache()
        with pytest.raises(VoiceConsentError):
            await record_consent(
                tenant_id="hotel-mzv",
                voice_owner_name="Zain",
                consent_artifact_url="https://example.com/consent.pdf",
                granted_by="zain@example.com",
            )

    async def test_error_response_raises(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="db error")

        client, _ = mock_http(handler)
        with pytest.raises(VoiceConsentError):
            await record_consent(
                tenant_id="hotel-mzv",
                voice_owner_name="Zain",
                consent_artifact_url="https://example.com/consent.pdf",
                granted_by="zain@example.com",
                client=client,
            )


class TestHasConsent:
    async def test_true_when_a_row_exists(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"id": 1, "tenant_id": "hotel-mzv"}])

        client, requests = mock_http(handler)
        assert await has_consent("hotel-mzv", client=client) is True
        assert dict(requests[0].url.params)["tenant_id"] == "eq.hotel-mzv"

    async def test_false_when_no_row(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client, _ = mock_http(handler)
        assert await has_consent("northside-plumbing", client=client) is False

    async def test_false_on_error_fails_closed(self):
        """A vault/network hiccup must never be read as "consent exists" —
        fail closed, same principle as the per-tenant secrets absent-vs-
        errored distinction elsewhere in Phase 4."""

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client, _ = mock_http(handler)
        assert await has_consent("hotel-mzv", client=client) is False

    async def test_false_without_supabase_configured(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        reset_settings_cache()
        assert await has_consent("hotel-mzv") is False


class TestCloneVoice:
    async def test_refuses_without_consent_and_never_calls_cartesia(self, audio_file):
        def consent_handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        def cartesia_handler(_req: httpx.Request) -> httpx.Response:
            raise AssertionError("clone_voice must not call Cartesia without consent")

        consent_client, _ = mock_http(consent_handler)
        cartesia_client, cartesia_requests = mock_http(cartesia_handler)

        with pytest.raises(VoiceConsentError):
            await clone_voice(
                tenant_id="hotel-mzv",
                audio_path=audio_file,
                voice_name="Zain's voice",
                consent_client=consent_client,
                cartesia_client=cartesia_client,
            )
        assert cartesia_requests == []

    async def test_request_shape_and_response_mapping(self, audio_file):
        """Note: an injected client carries whatever headers the test gives
        it (mirroring test_calcom_booking.py's `_mock_calcom` convention) —
        `clone_voice` only sets Authorization/Cartesia-Version when it builds
        its OWN client, which injection deliberately bypasses."""

        def consent_handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"id": 1}])

        def cartesia_handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/voices/clone"
            return httpx.Response(200, json={"id": "voice_abc123", "name": "Zain's voice"})

        consent_client, _ = mock_http(consent_handler)
        cartesia_client, cartesia_requests = mock_http(
            cartesia_handler,
            headers={"Authorization": "Bearer sk_car_test", "Cartesia-Version": "2026-03-01"},
        )

        voice_id = await clone_voice(
            tenant_id="hotel-mzv",
            audio_path=audio_file,
            voice_name="Zain's voice",
            consent_client=consent_client,
            cartesia_client=cartesia_client,
        )

        assert voice_id == "voice_abc123"
        assert len(cartesia_requests) == 1
        assert cartesia_requests[0].headers["authorization"] == "Bearer sk_car_test"
        assert b"not-real-audio-just-test-bytes" in cartesia_requests[0].content

    async def test_missing_cartesia_api_key_raises_before_any_cartesia_call(
        self, audio_file, monkeypatch
    ):
        monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
        reset_settings_cache()

        def consent_handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"id": 1}])

        consent_client, _ = mock_http(consent_handler)

        with pytest.raises(CartesiaError):
            await clone_voice(
                tenant_id="hotel-mzv",
                audio_path=audio_file,
                voice_name="Zain's voice",
                consent_client=consent_client,
            )

    async def test_cartesia_error_response_raises(self, audio_file):
        def consent_handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"id": 1}])

        def cartesia_handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad audio format")

        consent_client, _ = mock_http(consent_handler)
        cartesia_client, _ = mock_http(cartesia_handler)

        with pytest.raises(CartesiaError):
            await clone_voice(
                tenant_id="hotel-mzv",
                audio_path=audio_file,
                voice_name="Zain's voice",
                consent_client=consent_client,
                cartesia_client=cartesia_client,
            )
