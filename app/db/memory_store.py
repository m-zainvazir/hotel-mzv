"""In-memory system-of-record for Phase 1.

Deliberately implements the same protocols the Supabase store will, and keys
everything by `tenant_id` so the tenant-isolation tests are meaningful now
rather than only after Phase 4.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from functools import lru_cache
from threading import RLock

from app.db.models import (
    Call,
    CallSummary,
    ChatMessage,
    ChatSession,
    DailyMetrics,
    Escalation,
    Job,
    JobStatus,
    OutboundMessage,
    TenantMetrics,
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

    def list_chat_sessions(
        self, tenant_id: str, *, limit: int = 50, since: datetime | None = None
    ) -> list[ChatSession]:
        with self._lock:
            sessions = list(self._chat_sessions[tenant_id].values())
        if since is not None:
            sessions = [s for s in sessions if s.started_at >= since]
        return sorted(sessions, key=lambda s: s.started_at, reverse=True)[:limit]

    async def alist_chat_sessions(
        self, tenant_id: str, *, limit: int = 50, since: datetime | None = None
    ) -> list[ChatSession]:
        return self.list_chat_sessions(tenant_id, limit=limit, since=since)

    # --- analytics (Phase 8) ------------------------------------------------
    #
    # Python-side aggregation is fine here — it's the in-memory dev/test
    # store, not the network-bound SupabaseStore the plan's "rejected"
    # options were actually about. Every method walks the same five
    # collections `reset()` already clears.

    def tenant_metrics(self, tenant_id: str, *, since: date, until: date) -> TenantMetrics:
        with self._lock:
            calls = [
                c for c in self._calls[tenant_id].values() if since <= c.created_at.date() <= until
            ]
            # "Bookings", not "booking attempts": a cancelled job doesn't
            # count, the same distinction 0008_analytics.sql's SQL applies.
            jobs = [
                j
                for j in self._jobs[tenant_id].values()
                if since <= j.created_at.date() <= until and j.status is not JobStatus.CANCELLED
            ]
            escalations = [
                e for e in self._escalations[tenant_id] if since <= e.created_at.date() <= until
            ]
            chat_sessions = [
                s
                for s in self._chat_sessions[tenant_id].values()
                if since <= s.started_at.date() <= until
            ]
            chat_messages = [
                m for m in self._chat_messages[tenant_id] if since <= m.created_at.date() <= until
            ]
        return TenantMetrics(
            tenant_id=tenant_id,
            calls=len(calls),
            call_seconds=sum(c.duration_seconds or 0.0 for c in calls),
            cost_usd=sum(c.cost_usd or 0.0 for c in calls),
            jobs=len(jobs),
            escalations=len(escalations),
            chat_sessions=len(chat_sessions),
            chat_messages=len(chat_messages),
        )

    async def atenant_metrics(self, tenant_id: str, *, since: date, until: date) -> TenantMetrics:
        return self.tenant_metrics(tenant_id, since=since, until=until)

    def daily_series(self, tenant_id: str, *, since: date, until: date) -> list[DailyMetrics]:
        buckets: dict[date, DailyMetrics] = {}

        def _bucket(day: date) -> DailyMetrics:
            if day not in buckets:
                buckets[day] = DailyMetrics(day=day)
            return buckets[day]

        with self._lock:
            for call in self._calls[tenant_id].values():
                day = call.created_at.date()
                if since <= day <= until:
                    bucket = _bucket(day)
                    bucket.calls += 1
                    bucket.call_seconds += call.duration_seconds or 0.0
                    bucket.cost_usd += call.cost_usd or 0.0
            for job in self._jobs[tenant_id].values():
                day = job.created_at.date()
                if since <= day <= until and job.status is not JobStatus.CANCELLED:
                    _bucket(day).jobs += 1
            for escalation in self._escalations[tenant_id]:
                day = escalation.created_at.date()
                if since <= day <= until:
                    _bucket(day).escalations += 1
            for session in self._chat_sessions[tenant_id].values():
                day = session.started_at.date()
                if since <= day <= until:
                    _bucket(day).chat_sessions += 1
            for message in self._chat_messages[tenant_id]:
                day = message.created_at.date()
                if since <= day <= until:
                    _bucket(day).chat_messages += 1

        return [buckets[day] for day in sorted(buckets)]

    async def adaily_series(
        self, tenant_id: str, *, since: date, until: date
    ) -> list[DailyMetrics]:
        return self.daily_series(tenant_id, since=since, until=until)

    def list_recent_calls(
        self, tenant_id: str, *, limit: int = 50, since: datetime | None = None
    ) -> list[CallSummary]:
        calls = self.list_calls(tenant_id)
        if since is not None:
            calls = [c for c in calls if c.created_at >= since]
        calls = sorted(calls, key=lambda c: c.created_at, reverse=True)[:limit]
        return [_call_summary(c) for c in calls]

    async def alist_recent_calls(
        self, tenant_id: str, *, limit: int = 50, since: datetime | None = None
    ) -> list[CallSummary]:
        return self.list_recent_calls(tenant_id, limit=limit, since=since)

    def get_call(self, tenant_id: str, call_id: str) -> Call | None:
        with self._lock:
            return self._calls[tenant_id].get(call_id)

    async def aget_call(self, tenant_id: str, call_id: str) -> Call | None:
        return self.get_call(tenant_id, call_id)

    # --- test helper -------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._messages.clear()
            self._escalations.clear()
            self._calls.clear()
            self._chat_sessions.clear()
            self._chat_messages.clear()


def _call_summary(call: Call) -> CallSummary:
    return CallSummary(
        id=call.id,
        tenant_id=call.tenant_id,
        provider_call_id=call.provider_call_id,
        from_number=call.from_number,
        to_number=call.to_number,
        started_at=call.started_at,
        ended_at=call.ended_at,
        duration_seconds=call.duration_seconds,
        ended_reason=call.ended_reason,
        cost_usd=call.cost_usd,
        channel=call.channel,
        created_at=call.created_at,
    )


@lru_cache(maxsize=1)
def get_store() -> InMemoryStore:
    return InMemoryStore()
