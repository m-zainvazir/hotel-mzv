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
