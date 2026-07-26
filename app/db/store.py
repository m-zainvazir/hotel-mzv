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

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.db.models import Call, ChatMessage, ChatSession, Escalation, Job, OutboundMessage


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
