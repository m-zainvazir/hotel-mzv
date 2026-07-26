"""Twilio SMS notifier (Phase 3).

Talks to Twilio's REST API over a shared httpx client — no SDK, following the
precedent in `app/channels/vapi_provisioning.py::VapiClient`. Escalators
(warm transfer / SMS callback) live in `app/tools/messaging/transfer.py`, not
here — this file is purely the SMS carrier.

Reminder for whoever operates this: US SMS needs A2P 10DLC registration (days,
not minutes) or long-code traffic gets silently filtered — Twilio still
returns 201, so a missing text looks identical to a sent one on our side.
"""

from __future__ import annotations

import hashlib
import logging

import httpx

from app.config import Settings, get_settings
from app.db.factory import get_store
from app.db.models import OutboundMessage
from app.db.store import MessageLog
from app.tenancy.models import TenantConfig
from app.tenancy.secrets import TenantSecretError, resolve_secret
from app.tools.http_client import shared_async_client
from app.tools.messaging.base import MessagingError, Notifier

logger = logging.getLogger(__name__)

_MESSAGES_PATH = "/2010-04-01/Accounts/{sid}/Messages.json"


class TwilioNotifier(Notifier):
    name = "twilio"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        store: MessageLog | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._client = client
        self._store = store or get_store()
        self._settings = settings or get_settings()

    async def _get_client(self, tenant_id: str) -> tuple[httpx.AsyncClient, str]:
        """Returns (client, account_sid) — the sid is also a URL path
        parameter (`_MESSAGES_PATH`), not just part of the client's auth."""
        settings = self._settings
        if self._client is not None:
            # Test-injection path: no vault lookup, matching the old
            # behaviour exactly — sid must still come from *somewhere* to
            # build the request path, and tests rely on this failing with
            # zero HTTP calls when unconfigured.
            if not settings.twilio_account_sid:
                raise MessagingError("Twilio is not configured (TWILIO_ACCOUNT_SID unset)")
            return self._client, settings.twilio_account_sid

        # Per-tenant Vault secrets first, falling back to the shared env
        # credentials only when this tenant genuinely has none of its own
        # (Phase 4 Step 6) — never on a vault error, which would otherwise
        # silently text on behalf of whichever account the env credentials
        # belong to.
        try:
            account_sid = await resolve_secret(
                tenant_id, "twilio_account_sid", settings.twilio_account_sid
            )
            auth_token = await resolve_secret(
                tenant_id, "twilio_auth_token", settings.twilio_auth_token
            )
        except TenantSecretError as exc:
            raise MessagingError(
                "could not resolve this business's SMS credentials right now"
            ) from exc

        if not account_sid or not auth_token:
            raise MessagingError(
                "Twilio is not configured (TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN unset)"
            )

        # Fingerprint, never the raw token, in the cache key — this string
        # ends up in `shared_async_client`'s module-global dict and in tracebacks.
        fingerprint = hashlib.sha256(f"{account_sid}:{auth_token}".encode()).hexdigest()[:12]
        key = f"twilio:{tenant_id}:{fingerprint}"
        client = shared_async_client(
            key,
            base_url=settings.twilio_api_base,
            timeout=settings.twilio_timeout_seconds,
            auth=(account_sid, auth_token),
        )
        return client, account_sid

    async def send_sms(
        self,
        tenant: TenantConfig,
        *,
        to: str,
        body: str,
        kind: str = "confirmation",
    ) -> OutboundMessage:
        settings = self._settings
        client, sid = await self._get_client(tenant.tenant_id)

        if settings.twilio_messaging_service_sid:
            # Preferred: the routing A2P-registered traffic actually needs.
            sender_field = {"MessagingServiceSid": settings.twilio_messaging_service_sid}
        else:
            from_number = tenant.notifications.from_number or settings.twilio_from_number
            if not from_number:
                raise MessagingError(
                    "no Twilio sender configured — set TWILIO_MESSAGING_SERVICE_SID, "
                    "TWILIO_FROM_NUMBER, or the tenant's notifications.from_number"
                )
            sender_field = {"From": from_number}

        try:
            response = await client.post(
                _MESSAGES_PATH.format(sid=sid),
                data={"To": to, "Body": body, **sender_field},
            )
        except httpx.HTTPError as exc:
            await self._record_failure(tenant, to=to, body=body, kind=kind, error=str(exc))
            raise MessagingError(f"twilio request failed: {exc}") from exc

        if response.status_code >= 400:
            error = _error_text(response)
            await self._record_failure(tenant, to=to, body=body, kind=kind, error=error)
            raise MessagingError(f"twilio error: {error}")

        data = response.json()
        message = OutboundMessage(
            tenant_id=tenant.tenant_id,
            to=to,
            body=body,
            kind=kind,
            provider=self.name,
            provider_sid=data.get("sid"),
            status=data.get("status"),
        )
        logger.info(
            "twilio sms sent tenant=%s kind=%s sid=%s status=%s",
            tenant.tenant_id,
            kind,
            message.provider_sid,
            message.status,
        )
        return await self._store.arecord_message(message)

    async def _record_failure(
        self, tenant: TenantConfig, *, to: str, body: str, kind: str, error: str
    ) -> None:
        # Record the attempt before raising, so a failed send still shows up
        # in the audit trail rather than vanishing.
        await self._store.arecord_message(
            OutboundMessage(
                tenant_id=tenant.tenant_id,
                to=to,
                body=body,
                kind=kind,
                provider=self.name,
                status="failed",
                error=error,
            )
        )


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    code = data.get("code")
    detail = data.get("message") or data.get("detail") or ""
    return f"{code}: {detail}" if code else (detail or f"HTTP {response.status_code}")
