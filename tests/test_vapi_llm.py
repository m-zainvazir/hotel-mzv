"""The Vapi Custom-LLM shim.

Everything here runs offline against `ScriptedChatModel`. The payload shape is
the one from Vapi's own reference implementation, captured in
`tests/fixtures/vapi_chat_completion_request.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.channels.vapi_schema import VapiChatRequest, redacted
from app.config import get_settings, reset_settings_cache
from app.db.memory_store import get_store
from app.main import app
from tests.conftest import ai

FIXTURE = Path(__file__).parent / "fixtures" / "vapi_chat_completion_request.json"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def payload(**overrides) -> dict:
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    body.pop("_comment", None)
    body.update(overrides)
    return body


def parse_sse(text: str) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def spoken(text: str) -> str:
    """Concatenate everything that would reach the caller's ears.

    Skips frames with no `choices` — the `transferCall` function_call frame
    (`app/channels/vapi_schema.py::transfer_call_chunk`) isn't an OpenAI chunk
    shape at all; see `transfer_frame()` to inspect that one specifically.
    """
    return "".join(
        chunk["choices"][0]["delta"].get("content", "")
        for chunk in parse_sse(text)
        if "choices" in chunk
    )


def transfer_frame(text: str) -> dict | None:
    """The `{"function_call": {...}}` frame, if the turn requested a
    transfer — None otherwise."""
    for chunk in parse_sse(text):
        if "function_call" in chunk:
            return chunk
    return None


# --- wire format ------------------------------------------------------------


def test_stream_is_a_valid_openai_chunk_sequence(client, scripted, hotel):
    scripted(ai("We can get someone out to you today."))

    response = client.post("/chat/completions", json=payload())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.rstrip().endswith("data: [DONE]")

    chunks = parse_sse(response.text)
    assert chunks, "no chunks emitted"

    ids = {c["id"] for c in chunks}
    assert len(ids) == 1, "every chunk in a completion shares one id"

    for chunk in chunks:
        assert chunk["object"] == "chat.completion.chunk"
        assert chunk["choices"][0]["index"] == 0
        assert "delta" in chunk["choices"][0]

    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert [c["choices"][0]["finish_reason"] for c in chunks[:-1]] == [None] * (len(chunks) - 1)


def test_reply_streams_in_multiple_chunks(client, scripted, hotel):
    scripted(ai("Absolutely, we can help you with that this afternoon."))

    response = client.post("/chat/completions", json=payload())
    content_chunks = [
        c for c in parse_sse(response.text) if c["choices"][0]["delta"].get("content")
    ]

    assert len(content_chunks) > 3, "reply arrived whole — TTS would wait for all of it"
    assert "we can help you" in spoken(response.text)


def test_tool_output_never_reaches_the_caller(client, scripted, hotel):
    """Invariant 1: raw tool text must never be spoken aloud."""
    scripted(
        ai("Let me check. ", [{"name": "check_availability", "args": {"service": "diagnostic"}}]),
        ai("I can do this afternoon."),
    )

    said = spoken(client.post("/chat/completions", json=payload()).text)

    assert "slot_start_iso" not in said
    assert "Diagnostic visit (60 min" not in said
    assert "check_availability" not in said
    assert "I can do this afternoon." in said


def test_brain_failure_is_spoken_not_thrown(client, scripted, hotel):
    """Invariant 2: an HTTP error mid-call is silence. Say something instead."""
    scripted(RuntimeError("groq down"), RuntimeError("still down"))

    response = client.post("/chat/completions", json=payload())

    assert response.status_code == 200
    said = spoken(response.text)
    assert said.strip()
    assert response.text.rstrip().endswith("data: [DONE]")


def test_non_streaming_mode_returns_a_completion(client, scripted, hotel):
    scripted(ai("We open at eight."))

    response = client.post("/chat/completions", json=payload(stream=False))

    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert "We open at eight." in body["choices"][0]["message"]["content"]
    assert body["choices"][0]["finish_reason"] == "stop"


def test_malformed_body_is_a_400_not_a_500(client):
    assert client.post("/chat/completions", content=b"<html>").status_code == 400
    assert client.post("/chat/completions", json=["not", "an", "object"]).status_code == 400


def test_unknown_fields_do_not_break_a_live_call(client, scripted, hotel):
    """Vapi adds fields over time; a 422 mid-call would be unforgivable."""
    scripted(ai("Sure."))

    body = payload()
    body["someFutureVapiField"] = {"nested": True}
    body["call"]["brandNewThing"] = 42

    assert client.post("/chat/completions", json=body).status_code == 200


def test_missing_call_object_still_works(client, scripted, hotel):
    """`metadataSendMode: off` strips `call` — degrade, don't reject."""
    scripted(ai("Hello."))

    body = payload()
    body.pop("call")

    assert client.post("/chat/completions", json=body).status_code == 200


# --- tenancy ----------------------------------------------------------------


def test_assistant_id_selects_the_tenant(client, scripted, tmp_path, monkeypatch):
    """Assistant id wins: it maps to exactly one tenant."""
    from app.tenancy import loader
    from app.tenancy.repository import JsonFileTenantRepository

    data = tmp_path / "tenants"
    data.mkdir()
    source = get_settings().tenant_data_dir / "northside-plumbing.json"
    config = json.loads(source.read_text(encoding="utf-8"))
    config["vapi"] = {"assistant_id": "asst_northside"}
    (data / "northside-plumbing.json").write_text(json.dumps(config), encoding="utf-8")

    monkeypatch.setattr(loader, "_repository", JsonFileTenantRepository(data))
    loader.clear_tenant_cache()

    model = scripted(ai("Northside here."))
    body = payload()
    body["call"]["assistantId"] = "asst_northside"

    assert client.post("/chat/completions", json=body).status_code == 200
    assert "Northside Plumbing" in str(model.seen_prompts[0][0].content)


def test_falls_back_to_the_dialled_number(client, scripted, hotel):
    model = scripted(ai("Hotel_MZV here."))

    body = payload()
    body["call"]["assistantId"] = "asst_not_registered_yet"
    body["phoneNumber"]["number"] = "+15551230000"  # Hotel_MZV's number

    assert client.post("/chat/completions", json=body).status_code == 200
    assert "Hotel_MZV" in str(model.seen_prompts[0][0].content)


def test_a_tenant_id_in_the_body_is_ignored(client, scripted, hotel):
    """Invariant 3: the payload is attacker-controlled; tenancy is not."""
    model = scripted(ai("Hotel_MZV here."))

    body = payload()
    body["tenant_id"] = "northside-plumbing"
    body["metadata"] = {"tenant_id": "northside-plumbing"}

    assert client.post("/chat/completions", json=body).status_code == 200
    system_prompt = str(model.seen_prompts[0][0].content)
    assert "Hotel_MZV" in system_prompt
    assert "Northside Plumbing" not in system_prompt


# --- conversation threading -------------------------------------------------


def test_cold_thread_is_seeded_from_the_vapi_transcript(client, scripted, hotel):
    """A restart mid-call must not lose what the caller already said."""
    model = scripted(ai("Of course — what's the address?"))

    body = payload()
    body["messages"] = [
        {"role": "system", "content": "vapi-side prompt"},
        {"role": "user", "content": "I need a panel upgrade"},
        {"role": "assistant", "content": "Sure, when suits you?"},
        {"role": "user", "content": "Tuesday morning"},
    ]

    assert client.post("/chat/completions", json=body).status_code == 200

    prompt = model.seen_prompts[0]
    rendered = " ".join(str(m.content) for m in prompt)
    assert "I need a panel upgrade" in rendered
    assert "Tuesday morning" in rendered
    assert "vapi-side prompt" not in rendered, "Vapi's system prompt must be dropped"


def test_warm_thread_is_not_re_seeded(client, scripted, hotel):
    """Vapi resends everything each turn; our checkpointer already has it."""
    scripted(ai("First answer."), ai("Second answer."))

    first = payload()
    first["messages"] = [{"role": "user", "content": "my lights are out"}]
    client.post("/chat/completions", json=first)

    second = payload()
    second["messages"] = [
        {"role": "user", "content": "my lights are out"},
        {"role": "assistant", "content": "First answer."},
        {"role": "user", "content": "tomorrow works"},
    ]
    client.post("/chat/completions", json=second)

    from app.brain.llm import get_llm

    prompt = get_llm().seen_prompts[-1]
    said = [str(m.content) for m in prompt]
    assert said.count("my lights are out") == 1, "history was duplicated"


def test_each_call_gets_its_own_thread(client, scripted, hotel):
    model = scripted(ai("Answer one."), ai("Answer two."))

    first = payload()
    first["call"]["id"] = "call-aaa"
    first["messages"] = [{"role": "user", "content": "first caller here"}]
    client.post("/chat/completions", json=first)

    second = payload()
    second["call"]["id"] = "call-bbb"
    second["messages"] = [{"role": "user", "content": "second caller here"}]
    client.post("/chat/completions", json=second)

    second_prompt = " ".join(str(m.content) for m in model.seen_prompts[-1])
    assert "first caller here" not in second_prompt


def test_voice_channel_reaches_the_prompt(client, scripted, hotel):
    model = scripted(ai("Sure."))

    client.post("/chat/completions", json=payload())

    system_prompt = str(model.seen_prompts[0][0].content)
    assert "Channel: voice" in system_prompt
    assert "no lists, no markdown" in system_prompt


# --- auth -------------------------------------------------------------------


def test_vapi_endpoints_require_the_secret_when_configured(client, scripted, hotel, monkeypatch):
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", "sh4red")
    reset_settings_cache()
    try:
        scripted(ai("hi"), ai("hi"))

        assert client.post("/chat/completions", json=payload()).status_code == 401
        assert (
            client.post(
                "/chat/completions", json=payload(), headers={"x-vapi-secret": "wrong"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/chat/completions", json=payload(), headers={"x-vapi-secret": "sh4red"}
            ).status_code
            == 200
        )
    finally:
        monkeypatch.delenv("VAPI_WEBHOOK_SECRET", raising=False)
        reset_settings_cache()


def test_hmac_signature_is_accepted(client, scripted, hotel, monkeypatch):
    import hashlib
    import hmac as hmac_mod

    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", "sh4red")
    reset_settings_cache()
    try:
        scripted(ai("hi"))
        body = json.dumps(payload()).encode()
        signature = hmac_mod.new(b"sh4red", body, hashlib.sha256).hexdigest()

        response = client.post(
            "/chat/completions",
            content=body,
            headers={"content-type": "application/json", "x-vapi-signature": signature},
        )
        assert response.status_code == 200
    finally:
        monkeypatch.delenv("VAPI_WEBHOOK_SECRET", raising=False)
        reset_settings_cache()


# --- credential hygiene -----------------------------------------------------


def test_vapi_supplied_twilio_credentials_are_redacted():
    """Vapi puts live Twilio creds in `phoneNumber`; never log them raw."""
    raw = {
        "phoneNumber": {
            "number": "+15551230000",
            "twilioAccountSid": "AC_real",
            "twilioAuthToken": "tok_real",
        }
    }

    safe = redacted(raw)

    assert safe["phoneNumber"]["twilioAccountSid"] == "***"
    assert safe["phoneNumber"]["twilioAuthToken"] == "***"
    assert safe["phoneNumber"]["number"] == "+15551230000"
    assert raw["phoneNumber"]["twilioAuthToken"] == "tok_real", "must not mutate the original"


def test_committed_fixture_carries_no_real_credentials():
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert body["phoneNumber"]["twilioAccountSid"] == "***"
    assert body["phoneNumber"]["twilioAuthToken"] == "***"


# --- schema accessors -------------------------------------------------------


def test_schema_accessors_pull_the_right_numbers():
    request = VapiChatRequest.model_validate(payload())

    assert request.call_id == "b1f0c1e2-3a4b-4c5d-8e9f-0a1b2c3d4e5f"
    assert request.assistant_id == "asst_hotelmzv_test"
    assert request.dialled_number == "+15551230000"  # the tenant's line
    assert request.caller_number == "+15557654321"  # the person calling
    assert request.latest_user_text() == "I'd like to book a room for tonight"


def test_time_to_first_token_is_measured(client, scripted, hotel, caplog):
    """Our slice of the §13 budget has to be observable, or it can't be defended."""
    scripted(ai("We can help with that."))

    with caplog.at_level("INFO", logger="app.channels.vapi_llm"):
        client.post("/chat/completions", json=payload())

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "first token" in logged
    assert "ms" in logged


def test_slow_first_token_is_logged_as_a_warning(client, scripted, hotel, caplog, monkeypatch):
    monkeypatch.setattr("app.channels.vapi_llm.FIRST_TOKEN_BUDGET_MS", -1.0)
    scripted(ai("Slow."))

    with caplog.at_level("INFO", logger="app.channels.vapi_llm"):
        client.post("/chat/completions", json=payload())

    assert any(r.levelname == "WARNING" and "budget" in r.getMessage() for r in caplog.records)


def test_emergency_call_triggers_a_real_transfer_on_voice(client, scripted, hotel):
    """End to end on voice: Phase 3's WarmTransferEscalator is live by default,
    so an emergency call both speaks the handoff and emits the transferCall
    frame — after the spoken content, never before it (see _sse_chunks)."""
    scripted(
        ai(
            "That sounds dangerous, stay away from it. ",
            [{"name": "escalate", "args": {"reason": "sparks from the panel"}}],
        ),
        ai("Transferring you now."),
    )

    body = payload()
    body["messages"] = [{"role": "user", "content": "there are sparks from my breaker panel"}]

    text = client.post("/chat/completions", json=body).text
    said = spoken(text)
    frame = transfer_frame(text)

    assert "transferring" in said.lower()
    assert frame is not None
    assert frame["function_call"]["name"] == "transferCall"
    assert frame["function_call"]["arguments"]["destination"] == {
        "type": "number",
        "number": hotel.emergency.escalation_phone,
    }

    # The transfer frame is the second-to-last event, right before the
    # terminal `finish_reason: "stop"` chunk — after everything spoken.
    frames = parse_sse(text)
    assert "function_call" in frames[-2]
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"


def test_emergency_call_with_warm_transfer_disabled_never_emits_a_transfer_frame(
    client, scripted, hotel, override_tenant
):
    """Phase 5: `handoff` now fires on every channel (chat gets one too, for a
    click-to-call button), so the frame-emission decision has to hold even
    when a voice tenant opts out of warm transfer — the escalation still
    happens (SmsCallbackEscalator), but `data["transfer"]` is False and no
    transferCall frame may reach Vapi (app/channels/vapi_llm.py)."""
    override_tenant(
        hotel.model_copy(
            update={"emergency": hotel.emergency.model_copy(update={"allow_warm_transfer": False})}
        )
    )
    scripted(
        ai(
            "That sounds dangerous. ",
            [{"name": "escalate", "args": {"reason": "sparks from the panel"}}],
        ),
        ai("On-call has been alerted."),
    )

    body = payload()
    body["messages"] = [{"role": "user", "content": "there are sparks from my breaker panel"}]
    text = client.post("/chat/completions", json=body).text

    assert transfer_frame(text) is None
    assert "on-call has been alerted" in spoken(text).lower()


def test_no_calls_recorded_by_the_llm_endpoint(client, scripted, hotel):
    """Call records come from the end-of-call webhook, not the turn endpoint."""
    scripted(ai("hi"))
    client.post("/chat/completions", json=payload())
    assert get_store().list_calls(hotel.tenant_id) == []
