"""The `BookingProvider` seam (plan §10).

Google Calendar is the intended default; Cal.com and Supabase-native are
config flips. Swapping providers must never touch a graph node — hence this
interface takes and returns provider-neutral types only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel

from app.db.models import Job
from app.tenancy.models import Service, TenantConfig


class BookingError(RuntimeError):
    """Provider could not satisfy the request."""


class SlotUnavailableError(BookingError):
    """The requested start time is no longer bookable."""


class Slot(BaseModel):
    start: datetime
    end: datetime

    def label(self, tz=None) -> str:
        """Human phrasing, safe to speak aloud. Platform-independent (no %-d)."""
        start = self.start.astimezone(tz) if tz else self.start
        hour = start.hour % 12 or 12
        minute = f":{start.minute:02d}" if start.minute else ""
        meridiem = "am" if start.hour < 12 else "pm"
        return f"{start:%A} {start:%b} {start.day} at {hour}{minute}{meridiem}"


class ScheduleWindow(BaseModel):
    """One recurring "these days, these times" rule inside a schedule."""

    #: Full weekday names ("Monday"), in the order the provider gave them.
    days: list[str]
    #: 24-hour "HH:MM" wall clock in the schedule's own timezone.
    start: str
    end: str

    def label(self) -> str:
        # `_compress_days` returns "" for days it doesn't recognise (a provider
        # sending "Mon" rather than "Monday", say). Without the guard that
        # renders as a leading space, which reads as a formatting glitch in a
        # prompt and gets spoken as a pause aloud.
        days = _compress_days(self.days)
        return f"{days} {self.start}-{self.end}" if days else f"{self.start}-{self.end}"


class AvailabilitySchedule(BaseModel):
    """When a business is open, as the *authoritative* source describes it.

    Phase 9.4. Deliberately not `dict[str, DayHours]` (the tenant-config
    shape): a real calendar states recurring windows ("Mon-Fri 09:00-17:00"),
    which flattening into a per-day map would only re-expand for display.

    `source` is what lets the admin panel say where this came from without
    re-deriving it — "calcom" means the operator edits it in Cal.com,
    "config" means the manual grid in the panel is what the bot uses.
    """

    windows: list[ScheduleWindow] = []
    timezone: str = ""
    name: str = ""
    source: str = ""

    def summary(self) -> str:
        """One line, safe to speak aloud and safe to put in a prompt."""
        return ", ".join(window.label() for window in self.windows)


_WEEKDAY_ORDER = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _compress_days(days: list[str]) -> str:
    """ "Monday".."Friday" -> "Mon-Fri"; a gap stays a list ("Mon-Wed, Fri").

    Cal.com returns every day a rule covers, so an unabridged render of a
    seven-day schedule is 60-odd characters of noise in a prompt that pays
    for every token twice (once to the model, once in latency).
    """
    indexes = sorted({_WEEKDAY_ORDER.index(d) for d in days if d in _WEEKDAY_ORDER})
    if not indexes:
        return ""

    runs: list[list[int]] = [[indexes[0]]]
    for index in indexes[1:]:
        if index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])

    parts = []
    for run in runs:
        first, last = _WEEKDAY_ORDER[run[0]][:3], _WEEKDAY_ORDER[run[-1]][:3]
        parts.append(first if len(run) == 1 else f"{first}-{last}")
    return ", ".join(parts)


class BookingRequest(BaseModel):
    service_slug: str
    start: datetime
    customer_name: str
    customer_phone: str
    #: "" when the tenant doesn't require one (`BookingSettings.require_address`).
    address: str = ""
    #: Cal.com requires an attendee email; "" means the provider should
    #: synthesize a deterministic placeholder from `customer_phone`.
    customer_email: str = ""
    notes: str | None = None
    channel: str = "chat"


class BookingProvider(ABC):
    """All methods are tenant-scoped. Implementations must never cross tenants."""

    name: str = "base"

    @abstractmethod
    async def check_availability(
        self,
        tenant: TenantConfig,
        service: Service,
        *,
        earliest: datetime | None = None,
        limit: int | None = None,
    ) -> list[Slot]:
        """Return the next bookable slots for `service`, soonest first."""

    @abstractmethod
    async def create_booking(self, tenant: TenantConfig, request: BookingRequest) -> Job:
        """Create the calendar event and the authoritative `jobs` row."""

    @abstractmethod
    async def cancel(self, tenant: TenantConfig, job_id: str) -> Job:
        """Cancel an existing booking."""

    @abstractmethod
    async def reschedule(self, tenant: TenantConfig, job_id: str, new_start: datetime) -> Job:
        """Move an existing booking to a new start time."""

    async def availability_schedule(self, tenant: TenantConfig) -> AvailabilitySchedule | None:
        """This tenant's opening hours according to whoever owns them.

        Phase 9.4, and deliberately NOT abstract: a provider with no schedule
        of its own returns None and callers fall back. Adding it as an
        abstract method would break every third-party/test provider for a
        capability most of them can't have.

        Implementations may raise — `app/tools/booking/schedule.py` is the
        only caller and turns any failure into None, so a calendar outage
        degrades the prompt's hours line rather than the turn.
        """
        return None
