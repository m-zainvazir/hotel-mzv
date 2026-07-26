"""In-memory system-of-record for Phase 1.

Deliberately implements the same protocols the Supabase store will, and keys
everything by `tenant_id` so the tenant-isolation tests are meaningful now
rather than only after Phase 4.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from threading import RLock

from app.db.models import (
    Call,
    ChatMessage,
    ChatSession,
    Escalation,
    Job,
    JobStatus,
    OutboundMessage,
)


class InMemoryStore:
    """JobStore + MessageLog + EscalationLog, all tenant-scoped."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._jobs: dict[str, dict[str, Job]] = defaultdict(dict)
        self._messages: dict[str, list[OutboundMessage]] = defaultdict(list)
        self._escalations: dict[str, list[Escalation]] = defaultdict(list)
        self._calls: dict[str, dict[str, Call]] = defaultdict(dict)
        self._chat_sessions: dict[str, dict[str, ChatSession]] = defaultdict(dict)
        self._chat_messages: dict[str, list[ChatMessage]] = defaultdict(list)

    # --- jobs --------------------------------------------------------------

    def add(self, job: Job) -> Job:
        with self._lock:
            self._jobs[job.tenant_id][job.id] = job
        return job

    async def aadd(self, job: Job) -> Job:
        return self.add(job)

    def get(self, tenant_id: str, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs[tenant_id].get(job_id)

    async def aget(self, tenant_id: str, job_id: str) -> Job | None:
        return self.get(tenant_id, job_id)

    def list_jobs(
        self,
        tenant_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs[tenant_id].values())
        if since is not None:
            jobs = [j for j in jobs if j.scheduled_end > since]
        if until is not None:
            jobs = [j for j in jobs if j.scheduled_start < until]
        return sorted(jobs, key=lambda j: j.scheduled_start)

    async def alist_jobs(
        self,
        tenant_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Job]:
        return self.list_jobs(tenant_id, since=since, until=until)

    def update(self, job: Job) -> Job:
        with self._lock:
            if job.id not in self._jobs[job.tenant_id]:
                raise KeyError(f"job {job.id} not found for tenant {job.tenant_id}")
            self._jobs[job.tenant_id][job.id] = job
        return job

    async def aupdate(self, job: Job) -> Job:
        return self.update(job)

    def scheduled_between(self, tenant_id: str, start: datetime, end: datetime) -> list[Job]:
        return [
            job
            for job in self.list_jobs(tenant_id)
            if job.status is JobStatus.SCHEDULED and job.overlaps(start, end)
        ]

    async def ascheduled_between(self, tenant_id: str, start: datetime, end: datetime) -> list[Job]:
        return self.scheduled_between(tenant_id, start, end)

    # --- messages ----------------------------------------------------------

    def record_message(self, message: OutboundMessage) -> OutboundMessage:
        with self._lock:
            self._messages[message.tenant_id].append(message)
        return message

    async def arecord_message(self, message: OutboundMessage) -> OutboundMessage:
        return self.record_message(message)

    def list_messages(self, tenant_id: str) -> list[OutboundMessage]:
        with self._lock:
            return list(self._messages[tenant_id])

    async def alist_messages(self, tenant_id: str) -> list[OutboundMessage]:
        return self.list_messages(tenant_id)

    # --- calls -------------------------------------------------------------

    def record_call(self, call: Call) -> Call:
        """Upsert by provider call id — Vapi can resend an end-of-call report."""
        with self._lock:
            existing = next(
                (
                    c
                    for c in self._calls[call.tenant_id].values()
                    if c.provider_call_id == call.provider_call_id
                ),
                None,
            )
            if existing is not None:
                call = call.model_copy(update={"id": existing.id})
            self._calls[call.tenant_id][call.id] = call
        return call

    async def arecord_call(self, call: Call) -> Call:
        return self.record_call(call)

    def list_calls(self, tenant_id: str) -> list[Call]:
        with self._lock:
            calls = list(self._calls[tenant_id].values())
        return sorted(calls, key=lambda c: c.created_at)

    async def alist_calls(self, tenant_id: str) -> list[Call]:
        return self.list_calls(tenant_id)

    # --- escalations -------------------------------------------------------

    def record_escalation(self, escalation: Escalation) -> Escalation:
        with self._lock:
            self._escalations[escalation.tenant_id].append(escalation)
        return escalation

    async def arecord_escalation(self, escalation: Escalation) -> Escalation:
        return self.record_escalation(escalation)

    def list_escalations(self, tenant_id: str) -> list[Escalation]:
        with self._lock:
            return list(self._escalations[tenant_id])

    async def alist_escalations(self, tenant_id: str) -> list[Escalation]:
        return self.list_escalations(tenant_id)

    # --- chat transcripts (Phase 5) -----------------------------------------

    def start_chat_session(self, session: ChatSession) -> ChatSession:
        with self._lock:
            self._chat_sessions[session.tenant_id][session.id] = session
        return session

    async def astart_chat_session(self, session: ChatSession) -> ChatSession:
        return self.start_chat_session(session)

    def get_chat_session(self, tenant_id: str, session_id: str) -> ChatSession | None:
        with self._lock:
            return self._chat_sessions[tenant_id].get(session_id)

    async def aget_chat_session(self, tenant_id: str, session_id: str) -> ChatSession | None:
        return self.get_chat_session(tenant_id, session_id)

    def record_chat_message(self, message: ChatMessage) -> ChatMessage:
        with self._lock:
            self._chat_messages[message.tenant_id].append(message)
        return message

    async def arecord_chat_message(self, message: ChatMessage) -> ChatMessage:
        return self.record_chat_message(message)

    def list_chat_messages(self, tenant_id: str, session_id: str) -> list[ChatMessage]:
        with self._lock:
            messages = list(self._chat_messages[tenant_id])
        return sorted(
            (m for m in messages if m.session_id == session_id), key=lambda m: m.created_at
        )

    async def alist_chat_messages(self, tenant_id: str, session_id: str) -> list[ChatMessage]:
        return self.list_chat_messages(tenant_id, session_id)

    # --- test helper -------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._messages.clear()
            self._escalations.clear()
            self._calls.clear()
            self._chat_sessions.clear()
            self._chat_messages.clear()


@lru_cache(maxsize=1)
def get_store() -> InMemoryStore:
    return InMemoryStore()
