"""Storage protocols.

Every method takes `tenant_id` first and every implementation must scope by it
(CLAUDE.md convention #3). Phase 4 adds Supabase RLS underneath as
defence-in-depth — but the application layer never relies on that alone.

Each protocol declares both a sync method and an `a`-prefixed async twin
(`add`/`aadd`, `get`/`aget`, ...) — the `invoke`/`ainvoke` convention this
codebase already uses (see `ScriptedChatModel._stream`/`_astream` in
`tests/conftest.py`), not a rename. `InMemoryStore` keeps every sync method
exactly as it was and the async twins simply delegate — free for an
in-process dict, but the shape a network-backed store (Supabase over
PostgREST) actually needs, since blocking network I/O inside a sync call on
the booking critical path would stall the event loop against the §13 latency
budget. Application code (tools, channels, scripts) calls the async methods;
the sync methods exist for callers — today, only test fixtures — that run
outside an event loop.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from app.db.models import (
    Call,
    CallSummary,
    ChatMessage,
    ChatSession,
    DailyMetrics,
    Escalation,
    Job,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeHit,
    OutboundMessage,
    TenantMetrics,
)


@runtime_checkable
class JobStore(Protocol):
    def add(self, job: Job) -> Job: ...
    async def aadd(self, job: Job) -> Job: ...

    def get(self, tenant_id: str, job_id: str) -> Job | None: ...
    async def aget(self, tenant_id: str, job_id: str) -> Job | None: ...

    def list_jobs(
        self,
        tenant_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Job]: ...
    async def alist_jobs(
        self,
        tenant_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Job]: ...

    def update(self, job: Job) -> Job: ...
    async def aupdate(self, job: Job) -> Job: ...

    #: Conflict detection for booking providers (`StubBookingProvider`,
    #: `CalcomBookingProvider._require_job` callers). Declared on the
    #: protocol rather than left as an `InMemoryStore`-only extension, since
    #: any `JobStore` implementation needs a way to answer "what's already
    #: booked in this window" — a Supabase-backed store included.
    def scheduled_between(self, tenant_id: str, start: datetime, end: datetime) -> list[Job]: ...
    async def ascheduled_between(
        self, tenant_id: str, start: datetime, end: datetime
    ) -> list[Job]: ...


@runtime_checkable
class MessageLog(Protocol):
    def record_message(self, message: OutboundMessage) -> OutboundMessage: ...
    async def arecord_message(self, message: OutboundMessage) -> OutboundMessage: ...

    def list_messages(self, tenant_id: str) -> list[OutboundMessage]: ...
    async def alist_messages(self, tenant_id: str) -> list[OutboundMessage]: ...


@runtime_checkable
class CallLog(Protocol):
    def record_call(self, call: Call) -> Call: ...
    async def arecord_call(self, call: Call) -> Call: ...

    def list_calls(self, tenant_id: str) -> list[Call]: ...
    async def alist_calls(self, tenant_id: str) -> list[Call]: ...


@runtime_checkable
class EscalationLog(Protocol):
    def record_escalation(self, escalation: Escalation) -> Escalation: ...
    async def arecord_escalation(self, escalation: Escalation) -> Escalation: ...

    def list_escalations(self, tenant_id: str) -> list[Escalation]: ...
    async def alist_escalations(self, tenant_id: str) -> list[Escalation]: ...


@runtime_checkable
class ChatLog(Protocol):
    """Durable chat transcripts (Phase 5) — the chat-channel twin of `CallLog`.

    Not `MessageLog`: that protocol is `OutboundMessage` (SMS), a naming trap
    plan §6b fell into — see `app/db/migrations/0006_chat.sql`.
    """

    def start_chat_session(self, session: ChatSession) -> ChatSession: ...
    async def astart_chat_session(self, session: ChatSession) -> ChatSession: ...

    def get_chat_session(self, tenant_id: str, session_id: str) -> ChatSession | None: ...
    async def aget_chat_session(self, tenant_id: str, session_id: str) -> ChatSession | None: ...

    def record_chat_message(self, message: ChatMessage) -> ChatMessage: ...
    async def arecord_chat_message(self, message: ChatMessage) -> ChatMessage: ...

    def list_chat_messages(self, tenant_id: str, session_id: str) -> list[ChatMessage]: ...
    async def alist_chat_messages(self, tenant_id: str, session_id: str) -> list[ChatMessage]: ...

    #: Phase 8: the gap that made chat volume uncomputable — there was no way
    #: to enumerate a tenant's sessions at all, only to fetch one session's
    #: messages once you already had its id.
    def list_chat_sessions(
        self, tenant_id: str, *, limit: int = 50, since: datetime | None = None
    ) -> list[ChatSession]: ...
    async def alist_chat_sessions(
        self, tenant_id: str, *, limit: int = 50, since: datetime | None = None
    ) -> list[ChatSession]: ...


@runtime_checkable
class AnalyticsStore(Protocol):
    """Phase 8 — the admin dashboard's read surface.

    `SupabaseStore` reads these through the tenant-scoped JWT and the
    `security_invoker` views/RPC in `app/db/migrations/0008_analytics.sql`,
    never the secret key — a cross-tenant rollup is a per-tenant loop over
    this same protocol, one tenant's JWT at a time. That is what keeps a
    future logged-in tenant's own read of its own data on the identical code
    path an operator's read already uses (plans/phase8.md's tenant-login
    contract).
    """

    def tenant_metrics(self, tenant_id: str, *, since: date, until: date) -> TenantMetrics: ...
    async def atenant_metrics(
        self, tenant_id: str, *, since: date, until: date
    ) -> TenantMetrics: ...

    def daily_series(self, tenant_id: str, *, since: date, until: date) -> list[DailyMetrics]: ...
    async def adaily_series(
        self, tenant_id: str, *, since: date, until: date
    ) -> list[DailyMetrics]: ...

    #: Deliberately `CallSummary`, not `Call` — never a transcript in a list
    #: response. `aget_call` below is the only way to reach one.
    def list_recent_calls(
        self, tenant_id: str, *, limit: int = 50, since: datetime | None = None
    ) -> list[CallSummary]: ...
    async def alist_recent_calls(
        self, tenant_id: str, *, limit: int = 50, since: datetime | None = None
    ) -> list[CallSummary]: ...

    def get_call(self, tenant_id: str, call_id: str) -> Call | None: ...
    async def aget_call(self, tenant_id: str, call_id: str) -> Call | None: ...


@runtime_checkable
class KnowledgeStore(Protocol):
    """Per-bot RAG knowledge base (Phase 9 Part C) — the swap seam a future
    Qdrant/Pinecone implementation would slot into (`knowledge_source`,
    app/config.py). `tenant_id` first wherever the model itself doesn't
    already carry it, matching `JobStore`'s existing convention exactly.
    """

    def add_document(self, document: KnowledgeDocument) -> KnowledgeDocument: ...
    async def aadd_document(self, document: KnowledgeDocument) -> KnowledgeDocument: ...

    def list_documents(self, tenant_id: str) -> list[KnowledgeDocument]: ...
    async def alist_documents(self, tenant_id: str) -> list[KnowledgeDocument]: ...

    def get_document(self, tenant_id: str, document_id: str) -> KnowledgeDocument | None: ...
    async def aget_document(self, tenant_id: str, document_id: str) -> KnowledgeDocument | None: ...

    #: Cascades to that document's chunks (FK `on delete cascade`,
    #: 0011_knowledge.sql) — callers never need a separate chunk cleanup.
    def delete_document(self, tenant_id: str, document_id: str) -> None: ...
    async def adelete_document(self, tenant_id: str, document_id: str) -> None: ...

    def set_document_status(
        self,
        tenant_id: str,
        document_id: str,
        *,
        status: str,
        error: str | None = None,
        chunk_count: int | None = None,
    ) -> KnowledgeDocument: ...
    async def aset_document_status(
        self,
        tenant_id: str,
        document_id: str,
        *,
        status: str,
        error: str | None = None,
        chunk_count: int | None = None,
    ) -> KnowledgeDocument: ...

    #: A re-index replaces a document's whole chunk set — the caller
    #: (`app/rag/ingest.py`) deletes-then-inserts rather than this method
    #: diffing old vs. new chunks itself.
    def upsert_chunks(self, tenant_id: str, chunks: list[KnowledgeChunk]) -> None: ...
    async def aupsert_chunks(self, tenant_id: str, chunks: list[KnowledgeChunk]) -> None: ...

    def search_chunks(
        self,
        tenant_id: str,
        *,
        query_embedding: list[float],
        top_k: int,
        min_similarity: float,
    ) -> list[KnowledgeHit]: ...
    async def asearch_chunks(
        self,
        tenant_id: str,
        *,
        query_embedding: list[float],
        top_k: int,
        min_similarity: float,
    ) -> list[KnowledgeHit]: ...
