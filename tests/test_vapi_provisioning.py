"""Assistant payload construction — no network involved.

The point of most of these is the promise made in the Phase 2 plan: changing
the voice, or the TTS provider entirely, is config and never code.
"""

from __future__ import annotations

import json

import pytest

from app.channels.vapi_provisioning import (
    ProvisioningError,
    build_assistant_payload,
    build_voice,
    save_vapi_ids,
)
from app.config import Settings
from app.tenancy.models import TenantConfig


def settings(**overrides) -> Settings:
    base = {
        "public_base_url": "https://test.ngrok.app",
        "vapi_private_key": "vapi_test",
        "vapi_webhook_secret": "sh4red",
        "cartesia_default_voice_id": "voice_default",
    }
    base.update(overrides)
    return Settings(**base)


# --- voice is config, not code ---------------------------------------------


def test_voice_comes_from_tenant_config(hotel):
    tenant = hotel.model_copy(
        update={"voice": hotel.voice.model_copy(update={"voice_id": "v_abc"})}
    )

    voice = build_voice(tenant, settings())

    assert voice == {"provider": "cartesia", "model": "sonic-2", "voiceId": "v_abc"}


def test_unset_tenant_voice_inherits_the_env_default(hotel):
    assert build_voice(hotel, settings())["voiceId"] == "voice_default"


def test_tenant_voice_overrides_the_env_default(hotel):
    tenant = hotel.model_copy(
        update={"voice": hotel.voice.model_copy(update={"voice_id": "v_own"})}
    )

    assert build_voice(tenant, settings())["voiceId"] == "v_own"


def test_changing_the_voice_id_changes_only_the_voice_block(hotel):
    """The swap promised in the plan: one field, no code path."""
    first = build_assistant_payload(hotel, settings=settings(cartesia_default_voice_id="v_one"))
    second = build_assistant_payload(hotel, settings=settings(cartesia_default_voice_id="v_two"))

    assert first["voice"]["voiceId"] == "v_one"
    assert second["voice"]["voiceId"] == "v_two"

    del first["voice"], second["voice"]
    assert first == second, "changing a voice must not disturb anything else"


def test_the_tts_provider_itself_is_swappable(hotel):
    """Not just the voice — the whole provider is a config value."""
    tenant = hotel.model_copy(
        update={"voice": hotel.voice.model_copy(update={"provider": "11labs", "voice_id": "el_1"})}
    )

    voice = build_voice(tenant, settings())

    assert voice["provider"] == "11labs"
    assert voice["voiceId"] == "el_1"
    assert "model" not in voice, "sonic-2 is Cartesia-specific and must not leak"


def test_speed_is_only_sent_when_it_differs_from_default(hotel):
    assert "speed" not in build_voice(hotel, settings())

    faster = hotel.model_copy(update={"voice": hotel.voice.model_copy(update={"speed": 1.2})})
    assert build_voice(faster, settings())["speed"] == 1.2


def test_missing_voice_id_fails_loudly_at_provisioning_not_mid_call(hotel):
    with pytest.raises(ProvisioningError, match="voice_id"):
        build_voice(hotel, settings(cartesia_default_voice_id=None))


def test_no_voice_id_is_hard_coded_anywhere_in_the_payload(hotel):
    """Guards against someone inlining a voice during a debugging session."""
    payload = build_assistant_payload(hotel, settings=settings(cartesia_default_voice_id="v_uniq"))

    rendered = json.dumps(payload)
    assert rendered.count("v_uniq") == 1
    assert payload["voice"]["voiceId"] == "v_uniq"


# --- assistant payload ------------------------------------------------------


def test_assistant_points_at_our_endpoints(hotel):
    payload = build_assistant_payload(hotel, settings=settings())

    assert payload["model"]["provider"] == "custom-llm"
    # Vapi appends /chat/completions itself — don't include it here.
    assert payload["model"]["url"] == "https://test.ngrok.app"
    assert payload["server"]["url"] == "https://test.ngrok.app/webhooks/vapi"
    assert payload["server"]["secret"] == "sh4red"
    assert "end-of-call-report" in payload["serverMessages"]


def test_metadata_send_mode_is_left_at_the_default(hotel):
    """Setting it to `off` would strip `call` and break tenant resolution."""
    assert "metadataSendMode" not in build_assistant_payload(hotel, settings=settings())["model"]


def test_greeting_is_spoken_by_tts_not_generated(hotel):
    """Zero-latency first turn — no LLM round trip for the greeting."""
    payload = build_assistant_payload(hotel, settings=settings())
    assert payload["firstMessage"] == hotel.greeting
    assert "Hotel_MZV" in payload["firstMessage"]


def test_cost_guard_is_applied(hotel):
    payload = build_assistant_payload(hotel, settings=settings())
    assert payload["maxDurationSeconds"] == hotel.vapi.max_duration_seconds
    assert payload["silenceTimeoutSeconds"] == hotel.vapi.silence_timeout_seconds


def test_per_tenant_details_reach_the_assistant(hotel, northside):
    hotel_payload = build_assistant_payload(hotel, settings=settings())
    northside_payload = build_assistant_payload(northside, settings=settings())

    assert hotel_payload["name"] != northside_payload["name"]
    assert hotel_payload["firstMessage"] != northside_payload["firstMessage"]


@pytest.mark.parametrize("url", ["", "http://insecure.example", "not-a-url"])
def test_bad_public_base_url_is_rejected_before_calling_vapi(hotel, url):
    with pytest.raises(ProvisioningError):
        build_assistant_payload(hotel, settings=settings(public_base_url=url))


def test_secret_is_omitted_rather_than_sent_empty(hotel):
    payload = build_assistant_payload(hotel, settings=settings(vapi_webhook_secret=None))
    assert "secret" not in payload["server"]
    assert "headers" not in payload["model"]


def test_the_custom_llm_url_gets_its_own_credentials(hotel):
    """`server.secret` only covers webhooks to `server.url`.

    The custom-LLM endpoint is `model.url` — a different destination. Without
    a header there, every turn 401s and the caller hears the greeting then
    silence, which is a miserable thing to debug from a phone.
    """
    payload = build_assistant_payload(hotel, settings=settings(vapi_webhook_secret="sh4red"))

    assert payload["model"]["headers"] == {"x-vapi-secret": "sh4red"}
    assert payload["server"]["secret"] == "sh4red"


def test_both_endpoints_are_authenticated_with_the_same_secret(hotel):
    """One secret to configure, both destinations covered."""
    payload = build_assistant_payload(hotel, settings=settings(vapi_webhook_secret="only-one"))

    assert payload["model"]["headers"]["x-vapi-secret"] == payload["server"]["secret"]


# --- write-back -------------------------------------------------------------


def test_ids_are_written_back_into_the_tenant_file(tmp_path, hotel):
    path = tmp_path / f"{hotel.tenant_id}.json"
    path.write_text(json.dumps({"tenant_id": hotel.tenant_id}), encoding="utf-8")

    save_vapi_ids(hotel.tenant_id, data_dir=tmp_path, assistant_id="asst_1")
    save_vapi_ids(hotel.tenant_id, data_dir=tmp_path, phone_number_id="pn_1")

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["vapi"] == {"assistant_id": "asst_1", "phone_number_id": "pn_1"}


def test_detach_clears_the_number_but_keeps_the_assistant(tmp_path, hotel):
    path = tmp_path / f"{hotel.tenant_id}.json"
    path.write_text(
        json.dumps(
            {"tenant_id": hotel.tenant_id, "vapi": {"assistant_id": "a", "phone_number_id": "p"}}
        ),
        encoding="utf-8",
    )

    save_vapi_ids(hotel.tenant_id, data_dir=tmp_path, phone_number_id=None)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["vapi"]["phone_number_id"] is None
    assert written["vapi"]["assistant_id"] == "a", "web call must survive a detach"


def test_written_file_still_parses_as_a_tenant(tmp_path):
    source = json.loads((Settings().tenant_data_dir / "hotel-mzv.json").read_text(encoding="utf-8"))
    path = tmp_path / "hotel-mzv.json"
    path.write_text(json.dumps(source), encoding="utf-8")

    save_vapi_ids("hotel-mzv", data_dir=tmp_path, assistant_id="asst_round_trip")

    reloaded = TenantConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert reloaded.vapi.assistant_id == "asst_round_trip"
