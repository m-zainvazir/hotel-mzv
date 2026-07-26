"""Outbound messaging + escalation seams.

Twilio in Phase 3, Vapi warm transfer in Phase 2/3. The brain only ever sees
these interfaces, so swapping carriers is a config change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.db.models import Escalation, OutboundMessage
from app.tenancy.models import TenantConfig


class MessagingError(RuntimeError):
    pass


class Notifier(ABC):
    """Sends SMS/WhatsApp on behalf of a tenant."""

    name: str = "base"

    @abstractmethod
    async def send_sms(
        self,
        tenant: TenantConfig,
        *,
        to: str,
        body: str,
        kind: str = "confirmation",
    ) -> OutboundMessage: ...


class Escalator(ABC):
    """Hands a live conversation to a human.

    Voice → warm transfer; chat → SMS the on-call number and offer a callback.
    The graph asks for an escalation; the channel decides how it physically
    happens (plan §8).
    """

    name: str = "base"

    #: Can this implementation actually move the caller to a human mid-call?
    #:
    #: Read by the `escalate` tool to decide what the assistant is allowed to
    #: promise. It must never say "transferring you now" unless a transfer will
    #: really occur — on the emergency path that is the worst possible lie.
    #: Phase 3's `VapiTransferEscalator` flips this to True.
    can_transfer: bool = False

    @abstractmethod
    async def escalate(
        self,
        tenant: TenantConfig,
        *,
        reason: str,
        caller_summary: str = "",
        callback_number: str | None = None,
        channel: str = "chat",
    ) -> Escalation: ...
