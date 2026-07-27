"""Phase 1 messaging: record what *would* have been sent."""

from __future__ import annotations

import logging

from app.db.factory import get_store
from app.db.models import Escalation, OutboundMessage
from app.db.store import EscalationLog, MessageLog
from app.tenancy.models import TenantConfig
from app.tools.messaging.base import Escalator, Notifier

logger = logging.getLogger(__name__)


class StubNotifier(Notifier):
    name = "stub"

    def __init__(self, store: MessageLog | None = None) -> None:
        self._store = store or get_store()

    async def send_sms(
        self,
        tenant: TenantConfig,
        *,
        to: str,
        body: str,
        kind: str = "confirmation",
    ) -> OutboundMessage:
        message = OutboundMessage(
            tenant_id=tenant.tenant_id, to=to, body=body, kind=kind, provider=self.name
        )
        # DEBUG, not INFO: `body` carries the guest's name and appointment
        # time, and this is the *live* path for every tenant not on Twilio
        # (Phase 7 Step 5).
        logger.debug("[stub-sms] tenant=%s to=%s kind=%s: %s", tenant.tenant_id, to, kind, body)
        return await self._store.arecord_message(message)


class StubEscalator(Escalator):
    name = "stub"

    def __init__(self, store: EscalationLog | None = None) -> None:
        self._store = store or get_store()

    async def escalate(
        self,
        tenant: TenantConfig,
        *,
        reason: str,
        caller_summary: str = "",
        callback_number: str | None = None,
        channel: str = "chat",
    ) -> Escalation:
        escalation = Escalation(
            tenant_id=tenant.tenant_id,
            reason=reason,
            transferred_to=tenant.emergency.escalation_phone,
            caller_summary=caller_summary,
            callback_number=callback_number,
            channel=channel,
        )
        logger.warning(
            "[stub-escalate] tenant=%s to=%s reason=%s",
            tenant.tenant_id,
            escalation.transferred_to,
            reason,
        )
        return await self._store.arecord_escalation(escalation)
