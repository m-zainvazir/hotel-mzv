"""System-of-record rows.

Supabase holds the authoritative `jobs` row regardless of which calendar the
booking landed in (plan §10). Phase 1 keeps these in memory; the shapes are
what the Phase 4 tables will mirror.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
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


# --- analytics (Phase 8) -----------------------------------------------------
#
# Deliberately no per-tenant LLM cost or per-turn latency here — app/brain/
# metrics.py is process-global by design (its own docstring records that the
# ContextVar fix already failed), so that number isn't available and these
# models must not imply it is.


class TenantMetrics(BaseModel):
    """One tenant's totals over a caller-chosen window — the windowed bundle
    `public.tenant_metrics(from_day, to_day)` returns in one round trip."""

    tenant_id: str
    calls: int = 0
    call_seconds: float = 0.0
    #: Vapi telephony cost only — labelled narrowly on purpose in the
    #: dashboard, since showing *some* cost invites the reader to believe
    #: it's *the* cost (there is no LLM cost here).
    cost_usd: float = 0.0
    jobs: int = 0
    escalations: int = 0
    chat_sessions: int = 0
    chat_messages: int = 0


class DailyMetrics(BaseModel):
    """One day's row in a time-series chart — the same fields as
    `TenantMetrics`, bucketed by day instead of summed over the whole window.
    Mutable (unlike the frozen tenant-config models): store aggregation
    builds these up field-by-field as it walks rows/views."""

    day: date
    calls: int = 0
    call_seconds: float = 0.0
    cost_usd: float = 0.0
    jobs: int = 0
    escalations: int = 0
    chat_sessions: int = 0
    chat_messages: int = 0


class KnowledgeDocument(BaseModel):
    """One uploaded/pasted/crawled source (Phase 9 Part C) — the parent row
    a document's chunks hang off of. `status` tracks the background
    ingestion pipeline (`app/rag/ingest.py`): pending -> indexing ->
    ready|failed."""

    id: str = Field(default_factory=lambda: _new_id("kdoc"))
    tenant_id: str
    title: str = ""
    source_type: Literal["text", "file", "url"] = "text"
    #: The filename, URL, or a short label for a pasted block — display
    #: only, never re-fetched from this field.
    source_ref: str = ""
    status: Literal["pending", "indexing", "ready", "failed"] = "pending"
    error: str | None = None
    chunk_count: int = 0
    bytes: int = 0
    created_at: datetime = Field(default_factory=_utcnow)
    indexed_at: datetime | None = None


class KnowledgeChunk(BaseModel):
    """One embedded slice of a `KnowledgeDocument`. `embedding` is `None`
    until `app/rag/ingest.py` fills it in — a chunk can exist (and be
    displayed as "indexing") before its vector does."""

    id: str = Field(default_factory=lambda: _new_id("kchunk"))
    tenant_id: str
    document_id: str
    ordinal: int = 0
    content: str = ""
    token_count: int = 0
    embedding: list[float] | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class KnowledgeHit(BaseModel):
    """One `search_knowledge` / search-preview result — a chunk plus its
    similarity score and its parent document's title, so a caller never
    needs a second round trip just to attribute a quote to its source."""

    chunk_id: str
    document_id: str
    document_title: str
    content: str
    similarity: float


class CallSummary(BaseModel):
    """`Call` minus `transcript`/`recording_url` — the list-shaped analytics
    surface is PII-free *by construction*, not by convention: on
    `SupabaseStore` the excluded columns never leave the database at all
    (see `alist_recent_calls`'s explicit `select=`). `aget_call` is the only
    route to a transcript, one call at a time, on an explicit operator
    action."""

    id: str
    tenant_id: str
    provider_call_id: str
    from_number: str | None = None
    to_number: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: float | None = None
    ended_reason: str | None = None
    cost_usd: float | None = None
    channel: str = "voice"
    created_at: datetime = Field(default_factory=_utcnow)
