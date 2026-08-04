"""Supabase-backed store over PostgREST (Phase 4).

Every request carries the tenant-scoped JWT `app/db/auth.py` mints, so RLS
does the tenant-isolation work at the database layer — but every query below
still filters by `tenant_id` explicitly too, since CLAUDE.md convention #3
says the application layer must never rely on RLS alone.

Async-only: unlike `InMemoryStore`, there is no free synchronous form of a
network call, and nothing in the codebase calls a store's sync methods
outside test fixtures (which use the concrete `InMemoryStore` singleton
directly, not this class — see `tests/conftest.py`).

PostgREST semantics worth knowing before touching this file:
  * `Prefer: return=representation` on every write, and the *server's* row
    is what's returned — never the locally-constructed object, so a write
    that silently didn't land is never masked.
  * A `PATCH` matching zero rows returns 200 with `[]`, not 404 — `aupdate`
    turns that into `KeyError`, mirroring `InMemoryStore.update`.
  * Query values go through httpx's `params`, never an f-string — an
    ISO-8601 offset contains `+`, which is a literal space in a URL query.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx

from app.config import get_settings
from app.db.auth import tenant_jwt
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


class SupabaseStoreError(RuntimeError):
    """A PostgREST request failed (timeout, transport error, or 4xx/5xx)."""


def _rows(response: httpx.Response) -> list[dict[str, Any]]:
    if not response.content:
        return []
    data = response.json()
    return data if isinstance(data, list) else [data]


class SupabaseStore:
    """`JobStore` + `MessageLog` + `CallLog` + `EscalationLog`, over PostgREST.

    `client=` mirrors `CalcomBookingProvider`/`TwilioNotifier`: pass one in
    tests to inject an `httpx.MockTransport`; production leaves it unset and
    `_get_client()` falls back to the process-wide `shared_async_client`
    cache, keyed on the project URL alone — never on the per-tenant JWT,
    which travels as a per-request header instead. Folding a rotating token
    into the cache key would mint a new connection pool (and pay a cold TLS
    handshake) on every token refresh, exactly what `shared_async_client`
    exists to avoid.
    """

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client_override = client

    def _get_client(self) -> httpx.AsyncClient:
        if self._client_override is not None:
            return self._client_override
        # Local import: app.tools.http_client lives under the app.tools
        # package, whose __init__ eagerly imports the native tool registry
        # — which imports app.db.factory (this module's caller) at module
        # level. A top-level import here would risk a circular-import error
        # depending on which module happens to touch app.tools first.
        from app.tools.http_client import shared_async_client

        settings = get_settings()
        key = f"supabase-rest:{settings.supabase_url}"
        return shared_async_client(
            key,
            base_url=f"{settings.supabase_url}/rest/v1",
            headers={"apikey": settings.supabase_anon_key or ""},
            timeout=settings.supabase_timeout_seconds,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        tenant_id: str,
        params: dict[str, str] | list[tuple[str, str]] | None = None,
        json_body: dict[str, Any] | list[dict[str, Any]] | None = None,
        prefer: str | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {tenant_jwt(tenant_id)}"}
        if prefer:
            headers["Prefer"] = prefer
        client = self._get_client()
        try:
            response = await client.request(
                method, path, params=params, json=json_body, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise SupabaseStoreError(f"Supabase request timed out: {method} {path}") from exc
        except httpx.HTTPError as exc:
            raise SupabaseStoreError(f"Supabase request failed: {method} {path}: {exc}") from exc

        if response.status_code >= 400:
            raise SupabaseStoreError(
                f"Supabase {method} {path} -> {response.status_code}: {response.text[:300]}"
            )
        return response

    # --- jobs ----------------------------------------------------------

    async def aadd(self, job: Job) -> Job:
        response = await self._request(
            "POST",
            "/jobs",
            tenant_id=job.tenant_id,
            json_body=_job_to_row(job),
            prefer="return=representation",
        )
        rows = _rows(response)
        if not rows:
            raise SupabaseStoreError(f"insert into jobs returned no row for {job.id!r}")
        return _row_to_job(rows[0])

    async def aget(self, tenant_id: str, job_id: str) -> Job | None:
        response = await self._request(
            "GET",
            "/jobs",
            tenant_id=tenant_id,
            params={"tenant_id": f"eq.{tenant_id}", "id": f"eq.{job_id}", "limit": "1"},
        )
        rows = _rows(response)
        return _row_to_job(rows[0]) if rows else None

    async def alist_jobs(
        self,
        tenant_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Job]:
        # A list of tuples, not a dict — PostgREST needs repeated query keys
        # when both `since` and `until` are given, and a dict can only hold
        # one value per key.
        query: list[tuple[str, str]] = [
            ("tenant_id", f"eq.{tenant_id}"),
            ("order", "scheduled_start.asc"),
        ]
        if since is not None:
            query.append(("scheduled_end", f"gt.{since.isoformat()}"))
        if until is not None:
            query.append(("scheduled_start", f"lt.{until.isoformat()}"))
        response = await self._request("GET", "/jobs", tenant_id=tenant_id, params=query)
        return [_row_to_job(row) for row in _rows(response)]

    async def aupdate(self, job: Job) -> Job:
        response = await self._request(
            "PATCH",
            "/jobs",
            tenant_id=job.tenant_id,
            params={"tenant_id": f"eq.{job.tenant_id}", "id": f"eq.{job.id}"},
            json_body=_job_to_row(job),
            prefer="return=representation",
        )
        rows = _rows(response)
        if not rows:
            raise KeyError(f"job {job.id} not found for tenant {job.tenant_id}")
        return _row_to_job(rows[0])

    async def ascheduled_between(self, tenant_id: str, start: datetime, end: datetime) -> list[Job]:
        # Same overlap predicate as Job.overlaps: scheduled_start < end AND
        # scheduled_end > start, scoped to status=scheduled.
        query: list[tuple[str, str]] = [
            ("tenant_id", f"eq.{tenant_id}"),
            ("status", "eq.scheduled"),
            ("scheduled_start", f"lt.{end.isoformat()}"),
            ("scheduled_end", f"gt.{start.isoformat()}"),
        ]
        response = await self._request("GET", "/jobs", tenant_id=tenant_id, params=query)
        return [_row_to_job(row) for row in _rows(response)]

    # --- messages ----------------------------------------------------------

    async def arecord_message(self, message: OutboundMessage) -> OutboundMessage:
        response = await self._request(
            "POST",
            "/messages",
            tenant_id=message.tenant_id,
            json_body=_message_to_row(message),
            prefer="return=representation",
        )
        rows = _rows(response)
        if not rows:
            raise SupabaseStoreError(f"insert into messages returned no row for {message.id!r}")
        return _row_to_message(rows[0])

    async def alist_messages(self, tenant_id: str) -> list[OutboundMessage]:
        response = await self._request(
            "GET",
            "/messages",
            tenant_id=tenant_id,
            params={"tenant_id": f"eq.{tenant_id}", "order": "created_at.asc"},
        )
        return [_row_to_message(row) for row in _rows(response)]

    # --- calls ---------------------------------------------------------

    async def arecord_call(self, call: Call) -> Call:
        """Upsert by (tenant_id, provider_call_id) — Vapi can resend an
        end-of-call report. SELECT-then-write, not PostgREST's
        `resolution=merge-duplicates`: merge-duplicates overwrites every
        column sent, including our app-generated `id`, where the semantic we
        need (matching `InMemoryStore.record_call`) is "keep the existing id,
        replace the rest". This is a webhook, not the booking critical path,
        so the extra round-trip costs nothing that matters.
        """
        existing_response = await self._request(
            "GET",
            "/calls",
            tenant_id=call.tenant_id,
            params={
                "tenant_id": f"eq.{call.tenant_id}",
                "provider_call_id": f"eq.{call.provider_call_id}",
                "limit": "1",
            },
        )
        existing = _rows(existing_response)
        if existing:
            call = call.model_copy(update={"id": existing[0]["id"]})
            response = await self._request(
                "PATCH",
                "/calls",
                tenant_id=call.tenant_id,
                params={"tenant_id": f"eq.{call.tenant_id}", "id": f"eq.{call.id}"},
                json_body=_call_to_row(call),
                prefer="return=representation",
            )
        else:
            response = await self._request(
                "POST",
                "/calls",
                tenant_id=call.tenant_id,
                json_body=_call_to_row(call),
                prefer="return=representation",
            )
        rows = _rows(response)
        if not rows:
            raise SupabaseStoreError(f"upsert into calls returned no row for {call.id!r}")
        return _row_to_call(rows[0])

    async def alist_calls(self, tenant_id: str) -> list[Call]:
        response = await self._request(
            "GET",
            "/calls",
            tenant_id=tenant_id,
            params={"tenant_id": f"eq.{tenant_id}", "order": "created_at.asc"},
        )
        return [_row_to_call(row) for row in _rows(response)]

    # --- escalations -----------------------------------------------------

    async def arecord_escalation(self, escalation: Escalation) -> Escalation:
        response = await self._request(
            "POST",
            "/escalations",
            tenant_id=escalation.tenant_id,
            json_body=_escalation_to_row(escalation),
            prefer="return=representation",
        )
        rows = _rows(response)
        if not rows:
            raise SupabaseStoreError(
                f"insert into escalations returned no row for {escalation.id!r}"
            )
        return _row_to_escalation(rows[0])

    async def alist_escalations(self, tenant_id: str) -> list[Escalation]:
        response = await self._request(
            "GET",
            "/escalations",
            tenant_id=tenant_id,
            params={"tenant_id": f"eq.{tenant_id}", "order": "created_at.asc"},
        )
        return [_row_to_escalation(row) for row in _rows(response)]

    # --- chat transcripts (Phase 5) -----------------------------------------

    async def astart_chat_session(self, session: ChatSession) -> ChatSession:
        response = await self._request(
            "POST",
            "/chat_sessions",
            tenant_id=session.tenant_id,
            json_body=_chat_session_to_row(session),
            prefer="return=representation",
        )
        rows = _rows(response)
        if not rows:
            raise SupabaseStoreError(
                f"insert into chat_sessions returned no row for {session.id!r}"
            )
        return _row_to_chat_session(rows[0])

    async def aget_chat_session(self, tenant_id: str, session_id: str) -> ChatSession | None:
        response = await self._request(
            "GET",
            "/chat_sessions",
            tenant_id=tenant_id,
            params={"tenant_id": f"eq.{tenant_id}", "id": f"eq.{session_id}", "limit": "1"},
        )
        rows = _rows(response)
        return _row_to_chat_session(rows[0]) if rows else None

    async def arecord_chat_message(self, message: ChatMessage) -> ChatMessage:
        response = await self._request(
            "POST",
            "/chat_messages",
            tenant_id=message.tenant_id,
            json_body=_chat_message_to_row(message),
            prefer="return=representation",
        )
        rows = _rows(response)
        if not rows:
            raise SupabaseStoreError(
                f"insert into chat_messages returned no row for {message.id!r}"
            )
        return _row_to_chat_message(rows[0])

    async def alist_chat_messages(self, tenant_id: str, session_id: str) -> list[ChatMessage]:
        response = await self._request(
            "GET",
            "/chat_messages",
            tenant_id=tenant_id,
            params={
                "tenant_id": f"eq.{tenant_id}",
                "session_id": f"eq.{session_id}",
                "order": "created_at.asc",
            },
        )
        return [_row_to_chat_message(row) for row in _rows(response)]

    async def alist_chat_sessions(
        self, tenant_id: str, *, limit: int = 50, since: datetime | None = None
    ) -> list[ChatSession]:
        """Phase 8: the method that makes chat volume enumerable at all —
        `alist_chat_messages` above requires a `session_id` you'd have no way
        to obtain without this."""
        query: list[tuple[str, str]] = [
            ("tenant_id", f"eq.{tenant_id}"),
            ("order", "started_at.desc"),
            ("limit", str(limit)),
        ]
        if since is not None:
            query.append(("started_at", f"gte.{since.isoformat()}"))
        response = await self._request("GET", "/chat_sessions", tenant_id=tenant_id, params=query)
        return [_row_to_chat_session(row) for row in _rows(response)]

    # --- analytics (Phase 8) ---------------------------------------------
    #
    # Every read below goes through the tenant-scoped JWT `_request` already
    # mints, hitting the `security_invoker` views/RPC in
    # app/db/migrations/0008_analytics.sql — never the secret key. That is
    # what makes a future logged-in tenant's own read of its own metrics run
    # the identical code path an operator's cross-tenant loop already uses
    # (plans/phase8.md's tenant-login contract).

    async def atenant_metrics(self, tenant_id: str, *, since: date, until: date) -> TenantMetrics:
        response = await self._request(
            "POST",
            "/rpc/tenant_metrics",
            tenant_id=tenant_id,
            json_body={"from_day": since.isoformat(), "to_day": until.isoformat()},
        )
        rows = _rows(response)
        if not rows:
            return TenantMetrics(tenant_id=tenant_id)
        return _row_to_tenant_metrics(rows[0])

    async def adaily_series(
        self, tenant_id: str, *, since: date, until: date
    ) -> list[DailyMetrics]:
        # Four views, not one RPC: each is independently useful (the admin
        # API's breakdown panels query them directly too), and merging four
        # small per-tenant result sets client-side is cheaper than a fifth
        # bespoke "daily bundle" database object to keep in sync with them.
        query: list[tuple[str, str]] = [
            ("tenant_id", f"eq.{tenant_id}"),
            ("day", f"gte.{since.isoformat()}"),
            ("day", f"lte.{until.isoformat()}"),
        ]
        call_rows = _rows(
            await self._request("GET", "/daily_call_stats", tenant_id=tenant_id, params=query)
        )
        job_rows = _rows(
            await self._request("GET", "/daily_job_stats", tenant_id=tenant_id, params=query)
        )
        chat_rows = _rows(
            await self._request("GET", "/daily_chat_stats", tenant_id=tenant_id, params=query)
        )
        escalation_rows = _rows(
            await self._request("GET", "/daily_escalation_stats", tenant_id=tenant_id, params=query)
        )

        buckets: dict[date, DailyMetrics] = {}

        def _bucket(day_value: str) -> DailyMetrics:
            day = date.fromisoformat(str(day_value)[:10])
            if day not in buckets:
                buckets[day] = DailyMetrics(day=day)
            return buckets[day]

        for row in call_rows:
            bucket = _bucket(row["day"])
            bucket.calls += row.get("calls") or 0
            bucket.call_seconds += row.get("total_seconds") or 0.0
            bucket.cost_usd += row.get("cost_usd") or 0.0
        for row in job_rows:
            # daily_job_stats has one row per (day, status, channel) —
            # summing across every row for the same day gives the total, but
            # "bookings" means "still stands": a cancelled job doesn't count,
            # the same distinction the tenant_metrics() RPC applies.
            if row.get("status") == "cancelled":
                continue
            _bucket(row["day"]).jobs += row.get("jobs") or 0
        for row in chat_rows:
            bucket = _bucket(row["day"])
            bucket.chat_sessions += row.get("sessions") or 0
            bucket.chat_messages += row.get("messages") or 0
        for row in escalation_rows:
            _bucket(row["day"]).escalations += row.get("escalations") or 0

        return [buckets[day] for day in sorted(buckets)]

    async def alist_recent_calls(
        self, tenant_id: str, *, limit: int = 50, since: datetime | None = None
    ) -> list[CallSummary]:
        # Excluding transcript/recording_url in `select=` means they never
        # leave the database at all — a stronger guarantee than dropping
        # them client-side after the fact.
        query: list[tuple[str, str]] = [
            ("tenant_id", f"eq.{tenant_id}"),
            (
                "select",
                "id,tenant_id,provider_call_id,from_number,to_number,started_at,"
                "ended_at,duration_seconds,ended_reason,cost_usd,channel,created_at",
            ),
            ("order", "created_at.desc"),
            ("limit", str(limit)),
        ]
        if since is not None:
            query.append(("created_at", f"gte.{since.isoformat()}"))
        response = await self._request("GET", "/calls", tenant_id=tenant_id, params=query)
        return [_row_to_call_summary(row) for row in _rows(response)]

    async def aget_call(self, tenant_id: str, call_id: str) -> Call | None:
        response = await self._request(
            "GET",
            "/calls",
            tenant_id=tenant_id,
            params={"tenant_id": f"eq.{tenant_id}", "id": f"eq.{call_id}", "limit": "1"},
        )
        rows = _rows(response)
        return _row_to_call(rows[0]) if rows else None

    # --- knowledge base / RAG (Phase 9 Part C) ----------------------------
    #
    # Every method here goes through the tenant-scoped JWT `_request` already
    # mints — never the secret key. Deleting a tenant's own uploaded document
    # is ordinary tenant-scoped CRUD (0011_knowledge.sql grants `delete` on
    # these two tables specifically, unlike everywhere else in this schema),
    # not the cross-table destructive action `purge_tenant`
    # (app/tenancy/admin.py) is.

    async def aadd_document(self, document: KnowledgeDocument) -> KnowledgeDocument:
        response = await self._request(
            "POST",
            "/knowledge_documents",
            tenant_id=document.tenant_id,
            json_body=_knowledge_document_to_row(document),
            prefer="return=representation",
        )
        rows = _rows(response)
        if not rows:
            raise SupabaseStoreError(
                f"insert into knowledge_documents returned no row for {document.id!r}"
            )
        return _row_to_knowledge_document(rows[0])

    async def alist_documents(self, tenant_id: str) -> list[KnowledgeDocument]:
        response = await self._request(
            "GET",
            "/knowledge_documents",
            tenant_id=tenant_id,
            params={"tenant_id": f"eq.{tenant_id}", "order": "created_at.desc"},
        )
        return [_row_to_knowledge_document(row) for row in _rows(response)]

    async def aget_document(self, tenant_id: str, document_id: str) -> KnowledgeDocument | None:
        response = await self._request(
            "GET",
            "/knowledge_documents",
            tenant_id=tenant_id,
            params={"tenant_id": f"eq.{tenant_id}", "id": f"eq.{document_id}", "limit": "1"},
        )
        rows = _rows(response)
        return _row_to_knowledge_document(rows[0]) if rows else None

    async def adelete_document(self, tenant_id: str, document_id: str) -> None:
        # Chunks cascade (FK `on delete cascade`, 0011_knowledge.sql) — no
        # separate /knowledge_chunks call needed.
        await self._request(
            "DELETE",
            "/knowledge_documents",
            tenant_id=tenant_id,
            params={"tenant_id": f"eq.{tenant_id}", "id": f"eq.{document_id}"},
        )

    async def aset_document_status(
        self,
        tenant_id: str,
        document_id: str,
        *,
        status: str,
        error: str | None = None,
        chunk_count: int | None = None,
    ) -> KnowledgeDocument:
        body: dict[str, Any] = {"status": status, "error": error}
        if chunk_count is not None:
            body["chunk_count"] = chunk_count
        if status == "ready":
            body["indexed_at"] = datetime.now(UTC).isoformat()
        response = await self._request(
            "PATCH",
            "/knowledge_documents",
            tenant_id=tenant_id,
            params={"tenant_id": f"eq.{tenant_id}", "id": f"eq.{document_id}"},
            json_body=body,
            prefer="return=representation",
        )
        rows = _rows(response)
        if not rows:
            raise KeyError(f"knowledge document {document_id} not found for tenant {tenant_id}")
        return _row_to_knowledge_document(rows[0])

    async def aupsert_chunks(self, tenant_id: str, chunks: list[KnowledgeChunk]) -> None:
        """Always a plain multi-row insert, never an actual update — a
        re-index deletes the old chunk set first (`adelete_document` then a
        fresh `aadd_document`, driven by `app/rag/ingest.py`) rather than
        this method diffing old vs. new chunks. "Upsert" is the protocol's
        name for symmetry with the rest of this codebase's store methods,
        not a promise of ON CONFLICT semantics here."""
        if not chunks:
            return
        await self._request(
            "POST",
            "/knowledge_chunks",
            tenant_id=tenant_id,
            json_body=[_knowledge_chunk_to_row(chunk) for chunk in chunks],
            prefer="return=minimal",
        )

    async def asearch_chunks(
        self,
        tenant_id: str,
        *,
        query_embedding: list[float],
        top_k: int,
        min_similarity: float,
    ) -> list[KnowledgeHit]:
        # The RPC path every other analytics read already uses
        # (atenant_metrics, above) — tenant JWT, never the secret key.
        # match_knowledge_chunks (0011_knowledge.sql) has no tenant_id
        # parameter; RLS on the joined tables does the scoping.
        response = await self._request(
            "POST",
            "/rpc/match_knowledge_chunks",
            tenant_id=tenant_id,
            json_body={
                "query_embedding": query_embedding,
                "match_count": top_k,
                "min_similarity": min_similarity,
            },
        )
        return [_row_to_knowledge_hit(row) for row in _rows(response)]


# --- row <-> model mapping ---------------------------------------------------


def _job_to_row(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "tenant_id": job.tenant_id,
        "customer_name": job.customer_name,
        "customer_phone": job.customer_phone,
        "address": job.address,
        "service_slug": job.service_slug,
        "service_name": job.service_name,
        "scheduled_start": job.scheduled_start.isoformat(),
        "scheduled_end": job.scheduled_end.isoformat(),
        "status": job.status.value,
        "channel": job.channel,
        "calendar_event_id": job.calendar_event_id,
        "notes": job.notes,
        "created_at": job.created_at.isoformat(),
    }


def _row_to_job(row: dict[str, Any]) -> Job:
    return Job(
        id=row["id"],
        tenant_id=row["tenant_id"],
        customer_name=row["customer_name"],
        customer_phone=row["customer_phone"],
        address=row["address"],
        service_slug=row["service_slug"],
        service_name=row["service_name"],
        scheduled_start=row["scheduled_start"],
        scheduled_end=row["scheduled_end"],
        status=row["status"],
        channel=row["channel"],
        calendar_event_id=row.get("calendar_event_id"),
        notes=row.get("notes"),
        created_at=row["created_at"],
    )


def _message_to_row(message: OutboundMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "tenant_id": message.tenant_id,
        "to": message.to,
        "body": message.body,
        "kind": message.kind,
        "provider": message.provider,
        "provider_sid": message.provider_sid,
        "status": message.status,
        "error": message.error,
        "created_at": message.created_at.isoformat(),
    }


def _row_to_message(row: dict[str, Any]) -> OutboundMessage:
    return OutboundMessage(
        id=row["id"],
        tenant_id=row["tenant_id"],
        to=row["to"],
        body=row["body"],
        kind=row["kind"],
        provider=row["provider"],
        provider_sid=row.get("provider_sid"),
        status=row.get("status"),
        error=row.get("error"),
        created_at=row["created_at"],
    )


def _call_to_row(call: Call) -> dict[str, Any]:
    return {
        "id": call.id,
        "tenant_id": call.tenant_id,
        "provider_call_id": call.provider_call_id,
        "from_number": call.from_number,
        "to_number": call.to_number,
        "started_at": call.started_at.isoformat() if call.started_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
        "duration_seconds": call.duration_seconds,
        "ended_reason": call.ended_reason,
        "transcript": call.transcript,
        "recording_url": call.recording_url,
        "cost_usd": call.cost_usd,
        "channel": call.channel,
        "created_at": call.created_at.isoformat(),
    }


def _row_to_call(row: dict[str, Any]) -> Call:
    return Call(
        id=row["id"],
        tenant_id=row["tenant_id"],
        provider_call_id=row["provider_call_id"],
        from_number=row.get("from_number"),
        to_number=row.get("to_number"),
        started_at=row.get("started_at"),
        ended_at=row.get("ended_at"),
        duration_seconds=row.get("duration_seconds"),
        ended_reason=row.get("ended_reason"),
        transcript=row.get("transcript"),
        recording_url=row.get("recording_url"),
        cost_usd=row.get("cost_usd"),
        channel=row["channel"],
        created_at=row["created_at"],
    )


def _escalation_to_row(escalation: Escalation) -> dict[str, Any]:
    return {
        "id": escalation.id,
        "tenant_id": escalation.tenant_id,
        "reason": escalation.reason,
        "transferred_to": escalation.transferred_to,
        "caller_summary": escalation.caller_summary,
        "callback_number": escalation.callback_number,
        "channel": escalation.channel,
        "created_at": escalation.created_at.isoformat(),
    }


def _row_to_escalation(row: dict[str, Any]) -> Escalation:
    return Escalation(
        id=row["id"],
        tenant_id=row["tenant_id"],
        reason=row["reason"],
        transferred_to=row["transferred_to"],
        caller_summary=row["caller_summary"],
        callback_number=row.get("callback_number"),
        channel=row["channel"],
        created_at=row["created_at"],
    )


def _chat_session_to_row(session: ChatSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "tenant_id": session.tenant_id,
        "widget_key": session.widget_key,
        "origin": session.origin,
        "started_at": session.started_at.isoformat(),
    }


def _row_to_chat_session(row: dict[str, Any]) -> ChatSession:
    return ChatSession(
        id=row["id"],
        tenant_id=row["tenant_id"],
        widget_key=row.get("widget_key", ""),
        origin=row.get("origin"),
        started_at=row["started_at"],
    )


def _chat_message_to_row(message: ChatMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "tenant_id": message.tenant_id,
        "session_id": message.session_id,
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
    }


def _row_to_chat_message(row: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        id=row["id"],
        tenant_id=row["tenant_id"],
        session_id=row["session_id"],
        role=row["role"],
        content=row["content"],
        created_at=row["created_at"],
    )


def _row_to_call_summary(row: dict[str, Any]) -> CallSummary:
    return CallSummary(
        id=row["id"],
        tenant_id=row["tenant_id"],
        provider_call_id=row["provider_call_id"],
        from_number=row.get("from_number"),
        to_number=row.get("to_number"),
        started_at=row.get("started_at"),
        ended_at=row.get("ended_at"),
        duration_seconds=row.get("duration_seconds"),
        ended_reason=row.get("ended_reason"),
        cost_usd=row.get("cost_usd"),
        channel=row["channel"],
        created_at=row["created_at"],
    )


def _row_to_tenant_metrics(row: dict[str, Any]) -> TenantMetrics:
    return TenantMetrics(
        tenant_id=row["tenant_id"],
        calls=row.get("calls") or 0,
        call_seconds=row.get("call_seconds") or 0.0,
        cost_usd=row.get("cost_usd") or 0.0,
        jobs=row.get("jobs") or 0,
        escalations=row.get("escalations") or 0,
        chat_sessions=row.get("chat_sessions") or 0,
        chat_messages=row.get("chat_messages") or 0,
    )


def _knowledge_document_to_row(document: KnowledgeDocument) -> dict[str, Any]:
    return {
        "id": document.id,
        "tenant_id": document.tenant_id,
        "title": document.title,
        "source_type": document.source_type,
        "source_ref": document.source_ref,
        "status": document.status,
        "error": document.error,
        "chunk_count": document.chunk_count,
        "bytes": document.bytes,
        "created_at": document.created_at.isoformat(),
        "indexed_at": document.indexed_at.isoformat() if document.indexed_at else None,
    }


def _row_to_knowledge_document(row: dict[str, Any]) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=row["id"],
        tenant_id=row["tenant_id"],
        title=row.get("title") or "",
        source_type=row.get("source_type") or "text",
        source_ref=row.get("source_ref") or "",
        status=row.get("status") or "pending",
        error=row.get("error"),
        chunk_count=row.get("chunk_count") or 0,
        bytes=row.get("bytes") or 0,
        created_at=row["created_at"],
        indexed_at=row.get("indexed_at"),
    )


def _knowledge_chunk_to_row(chunk: KnowledgeChunk) -> dict[str, Any]:
    return {
        "id": chunk.id,
        "tenant_id": chunk.tenant_id,
        "document_id": chunk.document_id,
        "ordinal": chunk.ordinal,
        "content": chunk.content,
        "token_count": chunk.token_count,
        # pgvector's text input format IS a JSON-array-shaped literal
        # ("[0.1,0.2,...]"), so the plain JSON list httpx serializes here is
        # exactly what PostgREST forwards to Postgres for a `vector` column
        # — no extra encoding needed.
        "embedding": chunk.embedding,
        "created_at": chunk.created_at.isoformat(),
    }


def _row_to_knowledge_hit(row: dict[str, Any]) -> KnowledgeHit:
    return KnowledgeHit(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        document_title=row.get("document_title") or "",
        content=row.get("content") or "",
        similarity=row.get("similarity") or 0.0,
    )
