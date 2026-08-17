"""Phase 1 booking provider: real scheduling logic, fake calendar.

Slots respect the tenant's timezone, business hours, lead time and existing
bookings — so the conversation behaves exactly as it will in Phase 3, but
nothing leaves the process.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.db.factory import get_store
from app.db.models import Job, JobStatus
from app.db.store import JobStore
from app.tenancy.models import WEEKDAYS, Service, TenantConfig
from app.tools.booking.base import (
    AvailabilitySchedule,
    BookingProvider,
    BookingRequest,
    ScheduleWindow,
    Slot,
    SlotUnavailableError,
)


class StubBookingProvider(BookingProvider):
    name = "stub"

    def __init__(self, store: JobStore | None = None) -> None:
        self._store = store or get_store()

    # --- availability ------------------------------------------------------

    async def check_availability(
        self,
        tenant: TenantConfig,
        service: Service,
        *,
        earliest: datetime | None = None,
        limit: int | None = None,
    ) -> list[Slot]:
        limit = limit or tenant.booking.max_slots_returned
        granularity = timedelta(minutes=tenant.booking.slot_granularity_minutes)
        duration = timedelta(minutes=service.duration_minutes)

        now = datetime.now(tenant.tz)
        floor = now + timedelta(hours=tenant.booking.lead_time_hours)
        if earliest is not None:
            floor = max(floor, earliest.astimezone(tenant.tz))
        floor = _round_up(floor, granularity)

        # One query for the whole candidate window, not one per slot. Free
        # against an in-process dict, but a per-slot query would be hundreds
        # of round-trips against a network-backed store (Phase 4) on the
        # booking critical path — `Job.overlaps` below reproduces the same
        # per-slot conflict check against this already-fetched list instead.
        window_end = floor + timedelta(days=tenant.booking.horizon_days + 1)
        booked = await self._store.ascheduled_between(tenant.tenant_id, floor, window_end)

        slots: list[Slot] = []
        for offset in range(tenant.booking.horizon_days + 1):
            day = (floor + timedelta(days=offset)).date()
            hours = tenant.hours_for(day)
            if hours is None:
                continue

            open_at = datetime.combine(day, hours.open, tzinfo=tenant.tz)
            close_at = datetime.combine(day, hours.close, tzinfo=tenant.tz)
            cursor = _round_up(max(open_at, floor), granularity)

            while cursor + duration <= close_at:
                end = cursor + duration
                if not any(job.overlaps(cursor, end) for job in booked):
                    slots.append(Slot(start=cursor, end=end))
                    if len(slots) >= limit:
                        return slots
                cursor += granularity
        return slots

    # --- opening hours ---------------------------------------------------

    async def availability_schedule(self, tenant: TenantConfig) -> AvailabilitySchedule | None:
        """The manual grid in tenant config — which, for a stub tenant, IS the
        authoritative source (Phase 9.4). Every other provider answers from a
        real calendar; this one is why the grid still exists at all.

        Consecutive days sharing the same open/close collapse into one window,
        so a Mon-Fri business reads "Mon-Fri 09:00-17:00" here exactly as it
        would coming back from Cal.com.
        """
        windows: list[ScheduleWindow] = []
        for day in WEEKDAYS:
            hours = tenant.hours.get(day)
            if hours is None:
                continue
            start, end = f"{hours.open:%H:%M}", f"{hours.close:%H:%M}"
            if windows and windows[-1].start == start and windows[-1].end == end:
                windows[-1].days.append(day.capitalize())
            else:
                windows.append(ScheduleWindow(days=[day.capitalize()], start=start, end=end))

        if not windows:
            return None
        return AvailabilitySchedule(windows=windows, timezone=tenant.timezone, source="config")

    # --- mutations ---------------------------------------------------------

    async def create_booking(self, tenant: TenantConfig, request: BookingRequest) -> Job:
        service = tenant.service_by_slug(request.service_slug)
        if service is None:
            raise SlotUnavailableError(f"unknown service {request.service_slug!r}")

        start = request.start.astimezone(tenant.tz)
        end = start + timedelta(minutes=service.duration_minutes)

        if await self._store.ascheduled_between(tenant.tenant_id, start, end):
            raise SlotUnavailableError("that time was just taken")
        if not tenant.is_open_at(start):
            raise SlotUnavailableError(f"{start:%A %H:%M} is outside business hours")

        job = Job(
            tenant_id=tenant.tenant_id,
            customer_name=request.customer_name,
            customer_phone=request.customer_phone,
            address=request.address,
            service_slug=service.slug,
            service_name=service.name,
            scheduled_start=start,
            scheduled_end=end,
            channel=request.channel,
            calendar_event_id=f"stubcal_{start:%Y%m%dT%H%M}",
            notes=request.notes,
        )
        return await self._store.aadd(job)

    async def cancel(self, tenant: TenantConfig, job_id: str) -> Job:
        job = await self._require_job(tenant, job_id)
        return await self._store.aupdate(job.model_copy(update={"status": JobStatus.CANCELLED}))

    async def reschedule(self, tenant: TenantConfig, job_id: str, new_start: datetime) -> Job:
        job = await self._require_job(tenant, job_id)
        start = new_start.astimezone(tenant.tz)
        end = start + (job.scheduled_end - job.scheduled_start)

        overlapping = await self._store.ascheduled_between(tenant.tenant_id, start, end)
        clashes = [j for j in overlapping if j.id != job.id]
        if clashes:
            raise SlotUnavailableError("that time is already booked")

        return await self._store.aupdate(
            job.model_copy(update={"scheduled_start": start, "scheduled_end": end})
        )

    async def _require_job(self, tenant: TenantConfig, job_id: str) -> Job:
        job = await self._store.aget(tenant.tenant_id, job_id)
        if job is None:
            raise SlotUnavailableError(f"no job {job_id!r} for this business")
        return job


def _round_up(moment: datetime, step: timedelta) -> datetime:
    """Round `moment` up to the next multiple of `step` past midnight."""
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = moment - midnight
    steps = -(-elapsed // step)  # ceil division
    return midnight + steps * step
