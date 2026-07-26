"""Build and push the Vapi assistant for a tenant.

Separated from the CLI so the assistant payload can be unit-tested without
touching the network — which matters most for the voice block, since "changing
the voice is one line of config" is only true if nothing here hard-codes one.

Channel switching lives here too: the *same* assistant serves the browser web
call and the phone call. Attaching a number is what adds PSTN; nothing about
the assistant changes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from app.channels.vapi_schema import SECRET_HEADER, transfer_call_tool
from app.config import Settings, get_settings
from app.tenancy.models import TenantConfig

logger = logging.getLogger(__name__)

#: What we report to Vapi as the "model" name. Cosmetic, but shows in their logs.
MODEL_LABEL = "ai-receptionist"

#: Events we want delivered to /webhooks/vapi. Keep this tight — every event is
#: an HTTP round trip we have to authenticate and ignore.
SERVER_MESSAGES = ["end-of-call-report", "status-update"]


class ProvisioningError(RuntimeError):
    pass


# --- payload ----------------------------------------------------------------


def build_voice(tenant: TenantConfig, settings: Settings | None = None) -> dict[str, Any]:
    """The assistant's `voice` block, derived purely from config.

    Nothing downstream may branch on a specific voice or provider: swapping
    either is an edit to the tenant JSON plus a re-run of this script.
    """
    settings = settings or get_settings()
    voice_id = tenant.voice.voice_id or settings.cartesia_default_voice_id

    payload: dict[str, Any] = {"provider": tenant.voice.provider}
    if tenant.voice.provider == "cartesia":
        if not voice_id:
            raise ProvisioningError(
                f"tenant {tenant.tenant_id!r} has no voice.voice_id and "
                "CARTESIA_DEFAULT_VOICE_ID is unset — pick a voice at "
                "https://play.cartesia.ai and set one of them."
            )
        payload["model"] = tenant.voice.model
    if voice_id:
        payload["voiceId"] = voice_id
    if tenant.voice.speed != 1.0:
        payload["speed"] = tenant.voice.speed
    return payload


def build_assistant_payload(
    tenant: TenantConfig,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """The full Vapi assistant definition for one tenant."""
    settings = settings or get_settings()

    base_url = (settings.public_base_url or "").rstrip("/")
    if not base_url:
        raise ProvisioningError(
            "PUBLIC_BASE_URL is unset. Start a tunnel (`ngrok http 8000`) and put "
            "the https URL in .env before provisioning."
        )
    if not base_url.startswith("https://"):
        raise ProvisioningError(f"PUBLIC_BASE_URL must be https, got {base_url!r}")

    server: dict[str, Any] = {"url": f"{base_url}/webhooks/vapi"}
    model: dict[str, Any] = {
        # Vapi appends /chat/completions to this URL.
        "provider": "custom-llm",
        "url": base_url,
        "model": MODEL_LABEL,
        "temperature": settings.llm_temperature,
        # `metadataSendMode` is deliberately left at Vapi's default so the
        # `call` object (and therefore assistantId) arrives with each turn.
        # Setting it to "off" would break multi-tenant resolution.
    }

    if settings.vapi_webhook_secret:
        server["secret"] = settings.vapi_webhook_secret
        # `server.secret` only authenticates webhooks to `server.url`. The
        # custom-LLM endpoint is `model.url` — a different destination that
        # Vapi authenticates with custom headers. Without this, every turn
        # would 401 and the caller would hear the greeting then silence.
        model["headers"] = {SECRET_HEADER: settings.vapi_webhook_secret}

    if tenant.emergency.escalation_phone and tenant.emergency.allow_warm_transfer:
        # Declares the ONLY number(s) a live transfer may go to. Vapi refuses
        # to transfer anywhere not listed here — re-run this script after
        # changing escalation_phone or a real transfer silently no-ops
        # (plan Risk 8).
        model["tools"] = [transfer_call_tool([tenant.emergency.escalation_phone])]

    return {
        "name": f"{tenant.name} receptionist",
        # Spoken instantly by TTS with no LLM round trip — the cheapest latency
        # win available (plan §13).
        "firstMessage": tenant.greeting,
        "maxDurationSeconds": tenant.vapi.max_duration_seconds,
        "silenceTimeoutSeconds": tenant.vapi.silence_timeout_seconds,
        "model": model,
        "voice": build_voice(tenant, settings),
        "transcriber": {"provider": "deepgram", "model": "nova-2", "language": "en"},
        "server": server,
        "serverMessages": list(SERVER_MESSAGES),
    }


# --- API client -------------------------------------------------------------


class VapiClient:
    """Minimal Vapi REST client — only the calls provisioning needs."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        if not self._settings.vapi_private_key:
            raise ProvisioningError("VAPI_PRIVATE_KEY is unset — add it to .env.")
        self._client = httpx.Client(
            base_url=self._settings.vapi_api_base,
            headers={"Authorization": f"Bearer {self._settings.vapi_private_key}"},
            timeout=30.0,
        )

    def __enter__(self) -> VapiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise ProvisioningError(f"{method} {path} → {response.status_code}: {response.text}")
        return response.json() if response.content else {}

    def get_assistant(self, assistant_id: str) -> dict[str, Any] | None:
        try:
            return self._request("GET", f"/assistant/{assistant_id}")
        except ProvisioningError:
            return None

    def upsert_assistant(self, payload: dict[str, Any], assistant_id: str | None) -> dict[str, Any]:
        """Update in place when we already know the id, else create.

        Idempotence matters: the free ngrok URL rotates on every restart, so
        this runs often and must never leave a trail of duplicate assistants.
        """
        if assistant_id and self.get_assistant(assistant_id):
            return self._request("PATCH", f"/assistant/{assistant_id}", json=payload)
        return self._request("POST", "/assistant", json=payload)

    def import_twilio_number(
        self, number: str, assistant_id: str, sid: str, token: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/phone-number",
            json={
                "provider": "twilio",
                "number": number,
                "twilioAccountSid": sid,
                "twilioAuthToken": token,
                "assistantId": assistant_id,
            },
        )

    def create_vapi_number(self, assistant_id: str, area_code: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"provider": "vapi", "assistantId": assistant_id}
        if area_code:
            payload["numberDesiredAreaCode"] = area_code
        return self._request("POST", "/phone-number", json=payload)

    def attach_assistant_to_number(self, phone_number_id: str, assistant_id: str | None) -> dict:
        return self._request(
            "PATCH", f"/phone-number/{phone_number_id}", json={"assistantId": assistant_id}
        )


# --- tenant file write-back -------------------------------------------------


def save_vapi_ids(
    tenant_id: str,
    *,
    data_dir: Path,
    assistant_id: str | None = None,
    phone_number_id: str | None = ...,  # type: ignore[assignment]
) -> None:
    """Persist provisioning results back into the tenant's JSON.

    `phone_number_id` uses a sentinel default so that *not passing it* differs
    from explicitly clearing it (detach).
    """
    path = data_dir / f"{tenant_id}.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    vapi = dict(config.get("vapi") or {})

    if assistant_id is not None:
        vapi["assistant_id"] = assistant_id
    if phone_number_id is not ...:
        vapi["phone_number_id"] = phone_number_id

    config["vapi"] = vapi
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote vapi ids into %s", path.name)
