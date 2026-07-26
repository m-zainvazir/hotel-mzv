"""Escalation handoff strategies (Phase 3).

An `Escalator` decides HOW a live conversation reaches a human — it does not
perform the handoff itself. `WarmTransferEscalator.can_transfer = True` is a
*declaration*, read by `app/tools/messaging_tools.py::escalate` to decide what
the assistant is allowed to promise; the actual Vapi transfer is issued by the
voice channel adapter (`app/channels/vapi_llm.py`), the only place allowed to
speak Vapi's wire format for it (CLAUDE.md: no graph node names a vendor).
"""

from __future__ import annotations

from app.db.factory import get_store
from app.db.models import Escalation
from app.db.store import EscalationLog
from app.tenancy.models import TenantConfig
from app.tools.messaging.base import Escalator


class WarmTransferEscalator(Escalator):
    """Voice: a live transfer to the tenant's on-call number is possible."""

    name = "warm-transfer"
    can_transfer = True

    def __init__(self, store: EscalationLog | None = None) -> None:
        self._store = store or get_store()

    async def escalate(
        self,
        tenant: TenantConfig,
        *,
        reason: str,
        caller_summary: str = "",
        callback_number: str | None = None,
        channel: str = "voice",
    ) -> Escalation:
        escalation = Escalation(
            tenant_id=tenant.tenant_id,
            reason=reason,
            transferred_to=tenant.emergency.escalation_phone,
            caller_summary=caller_summary,
            callback_number=callback_number,
            channel=channel,
        )
        return await self._store.arecord_escalation(escalation)


class SmsCallbackEscalator(Escalator):
    """Chat (and voice tenants that opt out of warm transfer): no live
    handoff — alert on-call by SMS and offer a callback instead (plan §8)."""

    name = "sms-callback"
    can_transfer = False

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
        return await self._store.arecord_escalation(escalation)
