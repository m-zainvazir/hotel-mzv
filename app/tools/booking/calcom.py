"""Cal.com booking provider (Phase 3 — plan §10 amendment).

Cal.com books against "event types", not raw calendar slots. The default
shape for a tenant is ONE event type with multiple durations enabled, shared
across every service (`BookingSettings.event_type_id`); a service can
override this with its own `Service.event_type_id` if it needs its own
schedule/buffers/limits. `_event_type_for` below is the single place that
resolves which one applies, and `uses_shared_type` is the one flag that then
decides whether `duration` (slots) / `lengthInMinutes` (booking) get sent —
sending a duration a fixed-length event type doesn't offer is the documented
way to get zero slots back from Cal.com.

Cal.com, not our tenant JSON, is the source of truth for availability once a
tenant is on this provider: `check_availability` does not re-filter by
`booking.lead_time_hours` / `slot_granularity_minutes` — those become
prompt-only for calcom tenants (see `content/README.md`).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.db.factory import get_store
from app.db.models import Job
from app.db.store import JobStore
from app.tenancy.models import Service, TenantConfig
from app.tenancy.secrets import TenantSecretError, resolve_secret
from app.tools.booking.base import (
    AvailabilitySchedule,
    BookingError,
    BookingProvider,
    BookingRequest,
    ScheduleWindow,
    Slot,
    SlotUnavailableError,
)
from app.tools.http_client import shared_async_client

logger = logging.getLogger(__name__)

# Cal.com API v2 versions the *endpoint*, not the whole API — each family of
# endpoints pins its own header value.
_SLOTS_API_VERSION = "2024-09-04"
_BOOKINGS_API_VERSION = "2024-08-13"
_SCHEDULES_API_VERSION = "2024-06-11"

_SLOT_UNAVAILABLE_MARKERS = ("already booked", "no longer available", "slot", "conflict")


class CalcomBookingProvider(BookingProvider):
    name = "calcom"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        store: JobStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._client = client
        self._store = store or get_store()
        self._settings = settings or get_settings()

    # --- client --------------------------------------------------------

    async def _get_client(self, tenant_id: str) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        settings = self._settings

        # Per-tenant Vault secret first, falling back to the shared
        # CALCOM_API_KEY only when this tenant genuinely has none of its own
        # (Phase 4 Step 6) — never on a vault error, which would otherwise
        # silently book this tenant into whichever account CALCOM_API_KEY
        # belongs to.
        try:
            api_key = await resolve_secret(tenant_id, "calcom_api_key", settings.calcom_api_key)
        except TenantSecretError as exc:
            raise BookingError(
                "could not resolve this business's calendar credentials right now"
            ) from exc

        if not api_key:
            # Raised lazily, on first real use — not at construction — so a
            # misconfigured tenant produces a spoken "ERROR: ..." mid-turn
            # instead of a 500 the moment the graph builds its tools.
            raise BookingError("Cal.com is not configured (CALCOM_API_KEY unset)")

        # Fingerprint, never the raw key, in the cache key — this string ends
        # up in `shared_async_client`'s module-global dict and in tracebacks.
        fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:12]
        key = f"calcom:{tenant_id}:{fingerprint}:{settings.calcom_api_base}"
        return shared_async_client(
            key,
            base_url=settings.calcom_api_base,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=settings.calcom_timeout_seconds,
        )

    # --- event-type resolution ------------------------------------------

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

        params: dict[str, Any] = {
            "eventTypeId": event_type_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timeZone": tenant.timezone,
            "format": "range",
        }
        if uses_shared_type:
            params["duration"] = service.duration_minutes

        data = await self._request(
            "GET",
            "/slots",
            tenant_id=tenant.tenant_id,
            headers={"cal-api-version": _SLOTS_API_VERSION},
            params=params,
        )

        slots: list[Slot] = []
        for day_slots in (data or {}).values():
            for raw in day_slots:
                slot_start = _parse_calcom_datetime(raw["start"]).astimezone(tenant.tz)
                if slot_start < start:
                    continue
                raw_end = raw.get("end")
                slot_end = (
                    _parse_calcom_datetime(raw_end).astimezone(tenant.tz)
                    if raw_end
                    else slot_start + timedelta(minutes=service.duration_minutes)
                )
                slots.append(Slot(start=slot_start, end=slot_end))

        slots.sort(key=lambda s: s.start)
        if not slots:
            logger.warning(
                "calcom returned zero slots tenant=%s event_type=%s window=%s..%s — "
                "check the event type id and its Cal.com schedule",
                tenant.tenant_id,
                event_type_id,
                start.isoformat(),
                end.isoformat(),
            )
        return slots[:limit]

    # --- opening hours ---------------------------------------------------

    async def availability_schedule(self, tenant: TenantConfig) -> AvailabilitySchedule | None:
        """Phase 9.4: the account's own schedule, so the bot can answer "what
        time do you open?" from the same source that decides what it can
        actually book.

        Reads the tenant's *default* schedule rather than the event type's,
        because `GET /v2/event-types/{id}` returns only a `scheduleId` and
        resolving it would cost a second round trip for a value that is the
        default on every account this has been used against. A tenant whose
        event type deliberately runs on a non-default schedule will see the
        default one quoted here while `check_availability` keeps booking
        correctly against the real one — worth revisiting if that ever
        happens, and harmless until it does.
        """
        data = await self._request(
            "GET",
            "/schedules",
            tenant_id=tenant.tenant_id,
            headers={"cal-api-version": _SCHEDULES_API_VERSION},
        )
        return _schedule_from_calcom(data)

    # --- mutations ---------------------------------------------------------

    async def create_booking(self, tenant: TenantConfig, request: BookingRequest) -> Job:
        service = tenant.service_by_slug(request.service_slug)
        if service is None:
            raise BookingError(f"unknown service {request.service_slug!r}")

        if request.start.tzinfo is None:
            # A naive datetime here is a silent, DST-sized wrong booking —
            # refuse rather than guess.
            raise BookingError("booking start must be timezone-aware")

        event_type_id, uses_shared_type = self._event_type_for(tenant, service)
        settings = self._settings

        # Build the Job first (unpersisted) so job.id exists for `metadata` —
        # our only reconciliation handle if the POST below times out after
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

        body: dict[str, Any] = {
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
            body["lengthInMinutes"] = service.duration_minutes

        field_map = tenant.booking.booking_field_map
        responses = {}
        if request.address and "address" in field_map:
            responses[field_map["address"]] = request.address
        if request.notes and "notes" in field_map:
            responses[field_map["notes"]] = request.notes
        if responses:
            body["bookingFieldsResponses"] = responses

        data = await self._post_booking(tenant.tenant_id, body)

        status = data.get("status")
        if status in ("cancelled", "rejected"):
            raise BookingError(f"Cal.com booking was {status}")
        if status == "pending":
            logger.warning(
                "calcom booking is 'pending' (requires confirmation) tenant=%s uid=%s — "
                "the event type should be set to auto-confirm",
                tenant.tenant_id,
                data.get("uid"),
            )

        # Prefer Cal.com's own start/end over our arithmetic — if
        # lengthInMinutes was ignored, our record then matches the calendar
        # instead of a wrong assumption.
        api_start = (
            _parse_calcom_datetime(data["start"]).astimezone(tenant.tz)
            if data.get("start")
            else start_local
        )
        api_end = (
            _parse_calcom_datetime(data["end"]).astimezone(tenant.tz)
            if data.get("end")
            else job.scheduled_end
        )
        if api_start != start_local or api_end != job.scheduled_end:
            logger.warning(
                "calcom booking times differ from requested tenant=%s uid=%s "
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
                "calendar_event_id": data.get("uid"),
            }
        )
        # The local row stays authoritative (plan §10) — Cal.com's uid is a
        # link, not the record. send_confirmation looks jobs up here.
        return await self._store.aadd(job)

    async def cancel(self, tenant: TenantConfig, job_id: str) -> Job:
        from app.db.models import JobStatus

        job = await self._require_job(tenant, job_id)
        if job.calendar_event_id:
            await self._request(
                "POST",
                f"/bookings/{job.calendar_event_id}/cancel",
                tenant_id=tenant.tenant_id,
                headers={"cal-api-version": _BOOKINGS_API_VERSION},
                json={"cancellationReason": "cancelled by receptionist"},
            )
        return await self._store.aupdate(job.model_copy(update={"status": JobStatus.CANCELLED}))

    async def reschedule(self, tenant: TenantConfig, job_id: str, new_start: datetime) -> Job:
        job = await self._require_job(tenant, job_id)
        if new_start.tzinfo is None:
            raise BookingError("reschedule start must be timezone-aware")
        start = new_start.astimezone(tenant.tz)
        end = start + (job.scheduled_end - job.scheduled_start)

        if job.calendar_event_id:
            await self._request(
                "POST",
                f"/bookings/{job.calendar_event_id}/reschedule",
                tenant_id=tenant.tenant_id,
                headers={"cal-api-version": _BOOKINGS_API_VERSION},
                json={"start": start.astimezone(UTC).isoformat().replace("+00:00", "Z")},
            )
        return await self._store.aupdate(
            job.model_copy(update={"scheduled_start": start, "scheduled_end": end})
        )

    async def _require_job(self, tenant: TenantConfig, job_id: str) -> Job:
        job = await self._store.aget(tenant.tenant_id, job_id)
        if job is None:
            raise SlotUnavailableError(f"no job {job_id!r} for this business")
        return job

    # --- HTTP + error mapping ---------------------------------------------

    async def _post_booking(self, tenant_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST /bookings, with one specific self-healing retry.

        Verified against a live account: Cal.com 400s the *entire* request —
        even a `lengthInMinutes` that exactly matches the event type's own
        length — whenever that event type has no multiple-duration option
        enabled. There is no way to know that from our side in advance
        without an extra `GET /event-types/{id}` on every booking, so the
        first attempt is optimistic (send it, for tenants whose event type
        *does* support multiple durations) and this is the fallback for
        tenants whose don't. The scheduled_start/end reconciliation right
        after this call is what catches any resulting duration mismatch.
        """
        client = await self._get_client(tenant_id)
        response = await self._send_booking(client, body)

        if (
            response.status_code == 400
            and "lengthInMinutes" in body
            and _rejects_length_override(response)
        ):
            logger.warning(
                "calcom event_type=%s has no multiple-duration option — retrying "
                "the booking without lengthInMinutes",
                body.get("eventTypeId"),
            )
            retry_body = {k: v for k, v in body.items() if k != "lengthInMinutes"}
            response = await self._send_booking(client, retry_body)

        if response.status_code >= 400:
            self._raise_for_status(response)
        if not response.content:
            return {}
        payload = response.json()
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    async def _send_booking(
        self, client: httpx.AsyncClient, body: dict[str, Any]
    ) -> httpx.Response:
        try:
            return await client.post(
                "/bookings", headers={"cal-api-version": _BOOKINGS_API_VERSION}, json=body
            )
        except httpx.TimeoutException as exc:
            raise BookingError("the calendar didn't respond in time") from exc
        except httpx.HTTPError as exc:
            raise BookingError("could not reach the calendar") from exc

    async def _request(
        self, method: str, path: str, *, tenant_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        client = await self._get_client(tenant_id)
        try:
            response = await client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise BookingError("the calendar didn't respond in time") from exc
        except httpx.HTTPError as exc:
            raise BookingError("could not reach the calendar") from exc

        if response.status_code >= 400:
            self._raise_for_status(response)

        if not response.content:
            return {}
        payload = response.json()
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def _raise_for_status(self, response: httpx.Response) -> None:
        detail = _error_detail(response)
        if response.status_code in (401, 403):
            logger.error("calcom credentials rejected: %s", detail)
            raise BookingError("calendar credentials were rejected")
        if response.status_code == 404:
            logger.error("calcom 404 (bad event type?): %s", detail)
            raise BookingError("the calendar could not find that event type")
        if response.status_code == 409 or any(
            marker in detail.lower() for marker in _SLOT_UNAVAILABLE_MARKERS
        ):
            raise SlotUnavailableError("that time was just taken")
        if response.status_code >= 500:
            raise BookingError("the calendar is not responding")
        logger.error("calcom %s: %s", response.status_code, detail)
        raise BookingError("the calendar rejected that request")


def _rejects_length_override(response: httpx.Response) -> bool:
    """True iff the 400 is specifically Cal.com refusing lengthInMinutes on a
    fixed-length event type (verified error text: "Can't specify
    'lengthInMinutes' because event type does not have multiple possible
    lengths.") — narrow on purpose, so an unrelated 400 still surfaces."""
    detail = _error_detail(response).lower()
    return "lengthinminutes" in detail and "multiple" in detail


def _parse_calcom_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _schedule_from_calcom(data: Any) -> AvailabilitySchedule | None:
    """Map a Cal.com schedule payload onto the provider-neutral shape.

    Serves BOTH Cal.com transports, which hand over the same fields in
    different wrappers — confirmed live 2026-08-17 against this project's
    account:

    * REST `GET /v2/schedules` (`cal-api-version: 2024-06-11`) — a **list**,
      after `_request` strips the `{"status", "data"}` envelope.
    * MCP `get_default_schedule` — a **single dict**, because `_call_tool`'s
      `_unwrap_envelope` strips the identical envelope around one schedule.

    Normalising here rather than at each call site is what lets one mapper
    serve both. Getting it wrong is quiet, not loud: an unhandled shape
    returns None, the prompt simply omits the hours line, and nothing logs —
    which is exactly how the MCP path shipped with `schedule: null` until a
    live check caught it.

    `overrides` (one-off date exceptions) are deliberately ignored: they're
    "closed on the 25th", not opening hours, and `check_availability` already
    reflects them because Cal.com applies them to the slots it returns.
    """
    if isinstance(data, dict):
        # Either the envelope survived, or this is one bare schedule.
        inner = data.get("data", data)
        schedules = inner if isinstance(inner, list) else [inner]
    elif isinstance(data, list):
        schedules = data
    else:
        return None

    schedules = [s for s in schedules if isinstance(s, dict) and s.get("availability")]
    if not schedules:
        return None

    chosen = next((s for s in schedules if s.get("isDefault")), schedules[0])
    windows = [
        ScheduleWindow(
            days=list(rule.get("days") or []),
            start=str(rule.get("startTime") or ""),
            end=str(rule.get("endTime") or ""),
        )
        for rule in chosen.get("availability") or []
        if rule.get("days") and rule.get("startTime") and rule.get("endTime")
    ]
    if not windows:
        return None
    return AvailabilitySchedule(
        windows=windows,
        timezone=str(chosen.get("timeZone") or ""),
        name=str(chosen.get("name") or ""),
        source="calcom",
    )


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


def _error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(data.get("message") or error or data)
    return str(data)
