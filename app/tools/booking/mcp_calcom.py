"""Cal.com booking reached through its hosted MCP server (Phase 9 Part A).

Implements the unchanged `BookingProvider` ABC exactly like
`app/tools/booking/calcom.py` does — same event-type resolution, same
placeholder email, same metadata shape, same error-mapping table, same "local
`Job` row stays authoritative" rule. The only difference is transport: an MCP
tool call instead of a raw REST request. See plan §9 "Why the swap is at the
provider layer, not the tool tier" for why that line is drawn here rather
than by exposing Cal.com's tools to the model directly — nothing about
`check_availability` / `book_job` / `send_confirmation` changes.

**`get_availability` / `create_booking` argument shapes are confirmed live**
(2026-08-01, real hosted server, real Cal.com account, `hotel-mzv`'s own
calendar) — plan §9's live check 3, run end to end: a real availability
query returned real slots, a real booking landed on event type `6446177`
with the same duration/attendee-email pattern/metadata shape as an
equivalent `"calcom"`-provider booking, confirmed by pulling both bookings
back from Cal.com's own `/v2/bookings` API and comparing them directly.
`cancel_booking` / `reschedule_booking` remain UNVERIFIED — nothing in this
codebase calls `cancel`/`reschedule` yet (`plans/phase10.md` item 5), so
there's been no live call to confirm their shape the same way. If either
ever 400s once that lands, this is the first place to look.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any

from app.config import Settings, get_settings
from app.db.factory import get_store
from app.db.models import Job, JobStatus
from app.db.store import JobStore
from app.mcp.oauth import invalidate as invalidate_oauth_token
from app.tenancy.models import Service, TenantConfig
from app.tools.booking.base import (
    AvailabilitySchedule,
    BookingError,
    BookingProvider,
    BookingRequest,
    Slot,
    SlotUnavailableError,
)
from app.tools.booking.calcom import _schedule_from_calcom

logger = logging.getLogger(__name__)

_SLOT_UNAVAILABLE_MARKERS = ("already booked", "no longer available", "slot", "conflict")
_AUTH_FAILURE_MARKERS = ("unauthorized", "invalid_token", "401")

Connector = Callable[[dict[str, Any]], Any]


class McpBookingProvider(BookingProvider):
    name = "mcp_calcom"

    def __init__(
        self,
        *,
        session: Any = None,
        connector: Connector | None = None,
        store: JobStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        #: Test seam #1 — a ready-made fake session, bypassing OAuth,
        #: connection-building and the session cache entirely. Mirrors
        #: `CalcomBookingProvider(client=...)`.
        self._session_override = session
        #: Test seam #2 — replaces the real `streamablehttp_client` +
        #: `ClientSession` handshake with a fake connector, so caching
        #: behaviour and connection failures can be exercised without a
        #: real MCP transport.
        self._connector = connector or _default_connector
        self._store = store or get_store()
        self._settings = settings or get_settings()

    # --- session -------------------------------------------------------

    async def _session_for(self, tenant_id: str) -> Any:
        if self._session_override is not None:
            return self._session_override
        return await _get_session(tenant_id, self._settings, self._connector)

    # --- event-type resolution (verbatim from CalcomBookingProvider) ---

    def _event_type_for(self, tenant: TenantConfig, service: Service) -> tuple[int, bool]:
        """Return (event_type_id, uses_shared_type)."""
        if service.event_type_id is not None:
            return service.event_type_id, False
        if tenant.booking.event_type_id is not None:
            return tenant.booking.event_type_id, True
        raise BookingError(
            f"tenant {tenant.tenant_id!r} has no Cal.com event type configured for "
            f"service {service.slug!r} (set booking.event_type_id or "
            "the service's own event_type_id)"
        )

    # --- availability ----------------------------------------------------

    async def check_availability(
        self,
        tenant: TenantConfig,
        service: Service,
        *,
        earliest: datetime | None = None,
        limit: int | None = None,
    ) -> list[Slot]:
        event_type_id, uses_shared_type = self._event_type_for(tenant, service)
        limit = limit or tenant.booking.max_slots_returned

        start = (earliest or datetime.now(tenant.tz)).astimezone(tenant.tz)
        end = start + timedelta(days=tenant.booking.horizon_days)

        arguments: dict[str, Any] = {
            "eventTypeId": event_type_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timeZone": tenant.timezone,
        }
        if uses_shared_type:
            arguments["duration"] = service.duration_minutes

        session = await self._session_for(tenant.tenant_id)
        data = await _call_tool(tenant.tenant_id, session, "get_availability", arguments)

        slots: list[Slot] = []
        for day_slots in _iter_slot_groups(data):
            for raw in day_slots:
                slot_start = _parse_datetime(raw["start"]).astimezone(tenant.tz)
                if slot_start < start:
                    continue
                raw_end = raw.get("end")
                slot_end = (
                    _parse_datetime(raw_end).astimezone(tenant.tz)
                    if raw_end
                    else slot_start + timedelta(minutes=service.duration_minutes)
                )
                slots.append(Slot(start=slot_start, end=slot_end))

        slots.sort(key=lambda s: s.start)
        if not slots:
            logger.warning(
                "calcom mcp returned zero slots tenant=%s event_type=%s window=%s..%s",
                tenant.tenant_id,
                event_type_id,
                start.isoformat(),
                end.isoformat(),
            )
        return slots[:limit]

    # --- opening hours ---------------------------------------------------

    async def availability_schedule(self, tenant: TenantConfig) -> AvailabilitySchedule | None:
        """Phase 9.4: the account's own opening hours, over MCP.

        `get_default_schedule` takes no arguments and returns byte-identical
        JSON to REST's `GET /v2/schedules` single entry — confirmed live
        2026-08-17 against this project's account, so `_schedule_from_calcom`
        maps both without a second parser:

            {"status": "success", "data": {"id", "ownerId", "name",
             "timeZone", "availability": [{"days": [...], "startTime",
             "endTime"}], "isDefault", "overrides"}}

        Reads the *default* schedule for the same reason the REST provider
        does: resolving the event type's own `scheduleId` costs an extra
        round trip for a value that has been the default on every account
        this has run against.

        Note what is NOT done here: inferring hours from a `get_availability`
        sweep. That's the obvious fallback and it is actively wrong — a
        fully-booked Tuesday comes back empty, and the bot would tell callers
        it's closed on Tuesdays. Returning None (so the prompt says "check
        availability" instead of quoting hours) beats confident fiction.
        """
        session = await self._session_for(tenant.tenant_id)
        data = await _call_tool(tenant.tenant_id, session, "get_default_schedule", {})
        return _schedule_from_calcom(data)

    # --- mutations ---------------------------------------------------------

    async def create_booking(self, tenant: TenantConfig, request: BookingRequest) -> Job:
        service = tenant.service_by_slug(request.service_slug)
        if service is None:
            raise BookingError(f"unknown service {request.service_slug!r}")

        if request.start.tzinfo is None:
            raise BookingError("booking start must be timezone-aware")

        event_type_id, uses_shared_type = self._event_type_for(tenant, service)
        settings = self._settings

        # Build the Job first (unpersisted) so job.id exists for `metadata` —
        # our only reconciliation handle if the call below times out after
        # Cal.com actually created the booking.
        start_local = request.start.astimezone(tenant.tz)
        job = Job(
            tenant_id=tenant.tenant_id,
            customer_name=request.customer_name,
            customer_phone=request.customer_phone,
            address=request.address,
            service_slug=service.slug,
            service_name=service.name,
            scheduled_start=start_local,
            scheduled_end=start_local + timedelta(minutes=service.duration_minutes),
            channel=request.channel,
            notes=request.notes,
        )

        email = request.customer_email.strip() or _placeholder_email(
            request.customer_phone, settings
        )

        arguments: dict[str, Any] = {
            "start": request.start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "eventTypeId": event_type_id,
            "attendee": {
                "name": request.customer_name,
                "email": email,
                "timeZone": tenant.timezone,
                "phoneNumber": request.customer_phone,
            },
            "metadata": _metadata(tenant, job, request),
        }
        if uses_shared_type:
            arguments["lengthInMinutes"] = service.duration_minutes

        field_map = tenant.booking.booking_field_map
        responses = {}
        if request.address and "address" in field_map:
            responses[field_map["address"]] = request.address
        if request.notes and "notes" in field_map:
            responses[field_map["notes"]] = request.notes
        if responses:
            arguments["bookingFieldsResponses"] = responses

        session = await self._session_for(tenant.tenant_id)
        raw = await _call_tool(
            tenant.tenant_id, session, "create_booking", arguments, retry_without_length=True
        )
        data = raw if isinstance(raw, dict) else {}

        # A tool call that returns without `isError` is not proof Cal.com
        # actually created anything — found live (2026-08-03) against an
        # event_type_id that didn't exist in the account at all: the MCP
        # server returned a non-error response with no `uid`/`id` at all,
        # and this provider used to trust it anyway, silently recording a
        # local job with `calendar_event_id: None` and telling the caller
        # "booked" for something Cal.com never created. `uid`/`id` is the
        # only durable link back to a real calendar entry (see the module
        # docstring + plan §9's live check), so its absence is treated as a
        # failure, not a partial success — never claim a booking exists on
        # a caller's behalf without that proof.
        if not (data.get("uid") or data.get("id")):
            logger.error(
                "calcom mcp create_booking returned no uid/id tenant=%s event_type=%s "
                "— treating as a failed booking rather than trusting a partial response",
                tenant.tenant_id,
                event_type_id,
            )
            raise BookingError("the calendar did not confirm that booking")

        status = data.get("status")
        if status in ("cancelled", "rejected"):
            raise BookingError(f"Cal.com booking was {status}")
        if status == "pending":
            logger.warning(
                "calcom mcp booking is 'pending' (requires confirmation) tenant=%s uid=%s",
                tenant.tenant_id,
                data.get("uid"),
            )

        # Prefer Cal.com's own start/end over our arithmetic, same reasoning
        # as the REST provider — if lengthInMinutes was ignored, our record
        # then matches the calendar instead of a wrong assumption.
        api_start = (
            _parse_datetime(data["start"]).astimezone(tenant.tz)
            if data.get("start")
            else start_local
        )
        api_end = (
            _parse_datetime(data["end"]).astimezone(tenant.tz)
            if data.get("end")
            else job.scheduled_end
        )
        if api_start != start_local or api_end != job.scheduled_end:
            logger.warning(
                "calcom mcp booking times differ from requested tenant=%s uid=%s "
                "requested=%s..%s actual=%s..%s",
                tenant.tenant_id,
                data.get("uid"),
                start_local,
                job.scheduled_end,
                api_start,
                api_end,
            )

        job = job.model_copy(
            update={
                "scheduled_start": api_start,
                "scheduled_end": api_end,
                "calendar_event_id": data.get("uid") or data.get("id"),
            }
        )
        # The local row stays authoritative (plan §10) — Cal.com's uid is a
        # link, not the record. send_confirmation looks jobs up here.
        return await self._store.aadd(job)

    async def cancel(self, tenant: TenantConfig, job_id: str) -> Job:
        job = await self._require_job(tenant, job_id)
        if job.calendar_event_id:
            session = await self._session_for(tenant.tenant_id)
            await _call_tool(
                tenant.tenant_id,
                session,
                "cancel_booking",
                {
                    "bookingUid": job.calendar_event_id,
                    "cancellationReason": "cancelled by receptionist",
                },
            )
        return await self._store.aupdate(job.model_copy(update={"status": JobStatus.CANCELLED}))

    async def reschedule(self, tenant: TenantConfig, job_id: str, new_start: datetime) -> Job:
        job = await self._require_job(tenant, job_id)
        if new_start.tzinfo is None:
            raise BookingError("reschedule start must be timezone-aware")
        start = new_start.astimezone(tenant.tz)
        end = start + (job.scheduled_end - job.scheduled_start)

        if job.calendar_event_id:
            session = await self._session_for(tenant.tenant_id)
            await _call_tool(
                tenant.tenant_id,
                session,
                "reschedule_booking",
                {
                    "bookingUid": job.calendar_event_id,
                    "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                },
            )
        return await self._store.aupdate(
            job.model_copy(update={"scheduled_start": start, "scheduled_end": end})
        )

    async def _require_job(self, tenant: TenantConfig, job_id: str) -> Job:
        job = await self._store.aget(tenant.tenant_id, job_id)
        if job is None:
            raise SlotUnavailableError(f"no job {job_id!r} for this business")
        return job


# --- tool invocation + error mapping ----------------------------------------


async def _call_tool(
    tenant_id: str,
    session: Any,
    name: str,
    arguments: dict[str, Any],
    *,
    retry_without_length: bool = False,
) -> Any:
    """Returns whatever `_extract_payload` normalizes the result to — a dict
    for `create_booking`/`cancel_booking`/`reschedule_booking`, but possibly
    a list for `get_availability` (see `_iter_slot_groups`, which is what
    actually needs the list shape preserved). Callers that require a dict
    coerce it themselves.

    `retry_without_length` mirrors `app/tools/booking/calcom.py::_post_booking`'s
    self-healing retry: Cal.com 400s the *entire* booking — even a
    `lengthInMinutes` that exactly matches the event type's own length —
    whenever that event type has no multiple-duration option enabled, and
    there's no way to know that from our side in advance without an extra
    lookup on every booking. Only `create_booking` opts in; a bare
    `lengthInMinutes not in arguments` on the retry makes this self-limiting
    (no risk of looping).
    """
    try:
        result = await session.call_tool(name, arguments)
    except (Exception, asyncio.CancelledError) as exc:
        # Found live (2026-08-03): the mcp SDK's SSE read timeout
        # (`settings.calcom_mcp_timeout_seconds`) doesn't surface as a clean
        # TimeoutError — it fires by cancelling the pending read internally,
        # which anyio propagates up through `session.call_tool` as a bare
        # `asyncio.CancelledError`. That type deliberately does NOT subclass
        # `Exception` (Python 3.8+, precisely so an unrelated `except
        # Exception` can't accidentally swallow a real shutdown), so the
        # plain `except Exception` here used to let a slow-but-recoverable
        # timeout crash the whole graph turn (`NodeCancelledError`) instead
        # of degrading to a spoken apology like every other transport
        # failure in this codebase.
        await _drop_session(tenant_id)
        raise BookingError("could not reach the calendar") from exc

    payload = _extract_payload(result)
    if bool(getattr(result, "isError", False)):
        detail = _stringify(payload)
        if (
            retry_without_length
            and "lengthInMinutes" in arguments
            and _rejects_length_override(detail)
        ):
            logger.warning(
                "calcom mcp event_type=%s has no multiple-duration option — retrying "
                "the booking without lengthInMinutes",
                arguments.get("eventTypeId"),
            )
            retry_arguments = {k: v for k, v in arguments.items() if k != "lengthInMinutes"}
            return await _call_tool(tenant_id, session, name, retry_arguments)
        logger.error("calcom mcp tool %r error tenant=%s: %s", name, tenant_id, detail)
        if _looks_like_auth_failure(detail):
            await _drop_session(tenant_id)
        raise _map_tool_error(detail)
    return payload


def _rejects_length_override(detail: str) -> bool:
    """Mirrors `app/tools/booking/calcom.py::_rejects_length_override` — same
    verified Cal.com error text ("Can't specify 'lengthInMinutes' because
    event type does not have multiple possible lengths."), narrow on purpose
    so an unrelated error still surfaces normally."""
    lowered = detail.lower()
    return "lengthinminutes" in lowered and "multiple" in lowered


def _extract_payload(result: Any) -> Any:
    """Best-effort normalization of a `CallToolResult` — prefers
    `structuredContent` (structured tool output) and falls back to parsing
    the first text content block as JSON, then as plain text.

    Confirmed live (2026-08-03): Cal.com's hosted MCP server delivers its
    result via a JSON-encoded text content block, not `structuredContent`
    (observed `null`), wrapped in the exact same envelope its REST API uses —
    `{"status": "success"/"error", "data": {...}}` for a dict result, with
    the real fields (`uid`, `id`, `start`, `end`, ...) one level deeper than
    they first appear. `_unwrap_envelope` strips that envelope here, once,
    for every caller — mirrors `app/tools/booking/calcom.py`'s own
    `payload.get("data", payload)` convention over its direct REST calls. An
    error payload has no top-level `data` key (`{"status": "error", "error":
    {...}}`), so this is a no-op for the error path — `_call_tool`'s
    `_stringify`/error-mapping is unaffected.
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict | list):
        return _unwrap_envelope(structured)
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return _unwrap_envelope(json.loads(text))
            except ValueError:
                return text
    return {}


def _unwrap_envelope(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _iter_slot_groups(data: Any) -> list[list[dict[str, Any]]]:
    """Best-effort normalization of `get_availability`'s response into groups
    of `{start, end}` dicts — the exact shape is UNVERIFIED (see module
    docstring). Handles every shape Cal.com's REST `/slots` endpoint and a
    typical MCP wrapper around it could plausibly return: `{date: [...]}`,
    `{"slots": {date: [...]}}`, `{"data": {...}}`, or a flat list."""
    if isinstance(data, dict):
        data = data.get("slots", data.get("data", data))
    if isinstance(data, dict):
        return [group for group in data.values() if isinstance(group, list)]
    if isinstance(data, list):
        return [data]
    return []


def _stringify(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or payload)
    return str(payload)


def _looks_like_auth_failure(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in _AUTH_FAILURE_MARKERS)


def _map_tool_error(detail: str) -> BookingError:
    """Raw provider text never leaves this function — only into the log line
    `_call_tool` already wrote above."""
    lowered = detail.lower()
    if any(marker in lowered for marker in _SLOT_UNAVAILABLE_MARKERS):
        return SlotUnavailableError("that time was just taken")
    return BookingError("the calendar rejected that request")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _placeholder_email(phone: str, settings: Settings) -> str:
    """Deterministic so the same caller is always the same Cal.com attendee."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"caller-{digits}@{settings.booking_placeholder_email_domain}"


def _metadata(tenant: TenantConfig, job: Job, request: BookingRequest) -> dict[str, str]:
    raw = {
        "tenant_id": tenant.tenant_id,
        "job_id": job.id,
        "service_slug": request.service_slug,
        "channel": request.channel,
    }
    if request.address:
        raw["address"] = request.address
    # Cal.com caps metadata at 50 keys / 40-char keys / 500-char values.
    return {str(k)[:40]: str(v)[:500] for k, v in list(raw.items())[:50]}


# --- session connect + cache (Phase 9 Part A) -------------------------------

_session_lock = RLock()
#: tenant_id -> (created_at_monotonic, connection_fingerprint, exit_stack, session)
_session_cache: dict[str, tuple[float, str, AsyncExitStack, Any]] = {}


async def _connection_for(tenant_id: str, settings: Settings) -> dict[str, Any]:
    # Local imports: app.mcp.connections -> app.tenancy.secrets ->
    # app.tools.http_client sits under app.tools, whose __init__ eagerly
    # imports the native tool registry (this module included) — the same
    # "sits under app.tools" cycle every other provider in this package
    # avoids the same way (see app/tenancy/secrets.py's own note).
    from app.mcp.connections import build_connection
    from app.tenancy.models import McpServerConfig

    server = McpServerConfig(name="calcom", url=settings.calcom_mcp_url, auth="oauth")
    connection = await build_connection(tenant_id, server)
    if connection is None:
        # build_connection already logged a WARNING with the real cause
        # (no grant, a revoked one, a transport error resolving the token) —
        # a booking call must fail loudly, not silently drop the server the
        # way the long-tail MCP tool loader does.
        raise BookingError("could not resolve this business's calendar authorization right now")
    return connection


def _fingerprint(connection: dict[str, Any]) -> str:
    payload = json.dumps(connection, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


async def _get_session(tenant_id: str, settings: Settings, connector: Connector) -> Any:
    connection = await _connection_for(tenant_id, settings)
    fingerprint = _fingerprint(connection)
    now = time.monotonic()

    with _session_lock:
        cached = _session_cache.get(tenant_id)
    if (
        cached is not None
        and cached[1] == fingerprint
        and now - cached[0] < settings.calcom_mcp_session_cache_ttl_seconds
    ):
        return cached[3]

    stack = AsyncExitStack()
    try:
        session = await stack.enter_async_context(connector(connection))
    except BookingError:
        await stack.aclose()
        raise
    except Exception as exc:
        await stack.aclose()
        raise BookingError("could not reach the calendar") from exc

    old_stack: AsyncExitStack | None = None
    with _session_lock:
        existing = _session_cache.get(tenant_id)
        if existing is not None:
            old_stack = existing[2]
        _session_cache[tenant_id] = (now, fingerprint, stack, session)

    if old_stack is not None:
        await old_stack.aclose()

    return session


async def _drop_session(tenant_id: str) -> None:
    """Close and forget `tenant_id`'s cached session, and drop its cached
    OAuth access token too — call this whenever a live call fails in a way
    that suggests either is stale, so the *next* attempt reconnects and
    re-authenticates instead of repeating a failure this process already
    saw."""
    with _session_lock:
        existing = _session_cache.pop(tenant_id, None)
    if existing is not None:
        await existing[2].aclose()
    invalidate_oauth_token(tenant_id)


@asynccontextmanager
async def _default_connector(connection: dict[str, Any]):
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as exc:
        raise BookingError(
            'Cal.com MCP support is not installed — run `pip install -e ".[mcp]"`'
        ) from exc

    settings = get_settings()
    async with streamablehttp_client(
        connection["url"],
        headers=connection.get("headers"),
        timeout=settings.calcom_mcp_connect_timeout_seconds,
        sse_read_timeout=settings.calcom_mcp_timeout_seconds,
    ) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def aclose_calcom_mcp_sessions() -> None:
    """Call on process shutdown so open MCP sessions close cleanly.

    Mirrors `app/tools/http_client.py::close_shared_clients` for the same
    reason: a cached session's `AsyncExitStack` holds a live
    `streamablehttp_client` + `ClientSession` pair open indefinitely (that's
    the whole point of the cache), and nothing else in the process ever
    closes it. Left uncalled, the interpreter tears the still-open async
    generators down at GC/exit time instead — observed live as
    "attempted to exit cancel scope in a different task" / "generator is
    already running" noise on shutdown, harmless but a real gap all the
    same. `app/main.py`'s `lifespan` calls this alongside
    `close_shared_clients()`.
    """
    with _session_lock:
        stacks = [entry[2] for entry in _session_cache.values()]
        _session_cache.clear()
    for stack in stacks:
        await stack.aclose()


def reset_session_cache() -> None:
    """Test hook — drop cached MCP sessions without awaiting a close.

    Same rationale as `app/tools/http_client.py::reset_shared_clients`: tests
    run each with their own event loop, and a session cached across tests
    would raise on the next request against a closed loop.
    """
    with _session_lock:
        _session_cache.clear()
