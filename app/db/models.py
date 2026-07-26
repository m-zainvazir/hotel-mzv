"""System-of-record rows.

Supabase holds the authoritative `jobs` row regardless of which calendar the
booking landed in (plan §10). Phase 1 keeps these in memory; the shapes are
what the Phase 4 tables will mirror.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class JobStatus(StrEnum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class Job(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("job"))
    tenant_id: str
    customer_name: str
    customer_phone: str
    address: str
    service_slug: str
    service_name: str
    scheduled_start: datetime
    scheduled_end: datetime
    status: JobStatus = JobStatus.SCHEDULED
    channel: str = "chat"
    calendar_event_id: str | None = None
    notes: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)

    def overlaps(self, start: datetime, end: datetime) -> bool:
        if self.status is not JobStatus.SCHEDULED:
            return False
        return start < self.scheduled_end and end > self.scheduled_start


class OutboundMessage(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("msg"))
    tenant_id: str
    to: str
    body: str
    kind: str = "confirmation"  # confirmation | reminder | alert
    provider: str = "stub"
    #: Set by real notifiers (e.g. Twilio's message SID) for delivery lookups.
    provider_sid: str | None = None
    #: "sent" | "queued" | "failed" | ... — provider-defined, informational.
    status: str | None = None
    #: Set instead of raising when a send failed, so the attempt is still
    #: recorded (`send_confirmation`/`escalate` decide separately whether the
    #: failure should interrupt the conversation).
    error: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class Call(BaseModel):
    """One voice conversation, as reported by the orchestrator when it ends.

    Provider-neutral on purpose: `vapi_call_id` is the only vendor-shaped field,
    so swapping Vapi for Retell changes who fills this in, not its shape.
    """

    id: str = Field(default_factory=lambda: _new_id("call"))
    tenant_id: str
    provider_call_id: str
    from_number: str | None = None
    to_number: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: float | None = None
    ended_reason: str | None = None
    transcript: str | None = None
    recording_url: str | None = None
    cost_usd: float | None = None
    channel: str = "voice"
    created_at: datetime = Field(default_factory=_utcnow)


class ChatSession(BaseModel):
    """One widget conversation, started at the `/chat/session` handshake
    (Phase 5). `id` is the `session_id` minted there
    (`app/channels/widget_auth.py::new_session_id`), not the app-generated
    `<prefix>_<hex10>` id every other model here uses — chat_messages.session_id
    is a natural FK onto it.
    """

    id: str
    tenant_id: str
    widget_key: str = ""
    origin: str | None = None
    started_at: datetime = Field(default_factory=_utcnow)


class ChatMessage(BaseModel):
    """One turn's worth of transcript. Not `public.messages`
    (`OutboundMessage`, Phase 3) — that table is outbound SMS despite plan
    §6b's naming; this is the actual chat transcript (plan §6b's true
    intent), added in Phase 5 alongside `calls.transcript` for voice.
    """

    id: str = Field(default_factory=lambda: _new_id("chatmsg"))
    tenant_id: str
    session_id: str
    role: Literal["user", "assistant"]
    content: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class Escalation(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("esc"))
    tenant_id: str
    reason: str
    transferred_to: str
    caller_summary: str = ""
    callback_number: str | None = None
    channel: str = "chat"
    created_at: datetime = Field(default_factory=_utcnow)
