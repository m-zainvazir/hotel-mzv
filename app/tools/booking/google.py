"""Google Calendar booking provider — Phase 3.

Recommended default (plan §10). Notes for the implementer:
  * per-tenant OAuth tokens live encrypted in Supabase Vault, never in code;
  * MVP shortcut is a Workspace service account while Google verification runs;
  * `jobs` in Supabase stays authoritative — the calendar event id is a link,
    not the source of truth.
"""

from __future__ import annotations

from datetime import datetime

from app.db.models import Job
from app.tenancy.models import Service, TenantConfig
from app.tools.booking.base import BookingProvider, BookingRequest, Slot


class GoogleCalendarBookingProvider(BookingProvider):  # pragma: no cover - Phase 3
    name = "google"

    async def check_availability(
        self,
        tenant: TenantConfig,
        service: Service,
        *,
        earliest: datetime | None = None,
        limit: int | None = None,
    ) -> list[Slot]:
        raise NotImplementedError("Google Calendar provider lands in Phase 3 (plan §15).")

    async def create_booking(self, tenant: TenantConfig, request: BookingRequest) -> Job:
        raise NotImplementedError("Google Calendar provider lands in Phase 3 (plan §15).")

    async def cancel(self, tenant: TenantConfig, job_id: str) -> Job:
        raise NotImplementedError("Google Calendar provider lands in Phase 3 (plan §15).")

    async def reschedule(self, tenant: TenantConfig, job_id: str, new_start: datetime) -> Job:
        raise NotImplementedError("Google Calendar provider lands in Phase 3 (plan §15).")
