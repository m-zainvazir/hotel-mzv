"""Tenant profile — the per-business configuration the brain reasons with.

In Phase 1 these come from JSON files; in Phase 4 the same models are hydrated
from Supabase rows. Nothing downstream should care which.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WEEKDAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


class DayHours(BaseModel):
    """Opening hours for one weekday, in the tenant's local timezone."""

    model_config = ConfigDict(frozen=True)

    open: time
    close: time

    @model_validator(mode="after")
    def _close_after_open(self) -> DayHours:
        if self.close <= self.open:
            raise ValueError(f"close ({self.close}) must be after open ({self.open})")
        return self


class Service(BaseModel):
    """One bookable service in the tenant's catalogue."""

    model_config = ConfigDict(frozen=True)

    slug: str
    name: str
    description: str = ""
    duration_minutes: int = Field(default=60, gt=0, le=8 * 60)
    price_usd: float | None = None
    emergency: bool = False
    aliases: list[str] = Field(default_factory=list)
    #: Cal.com event-type override for this one service. When unset, booking
    #: falls back to `BookingSettings.event_type_id` (the tenant-wide
    #: multiple-duration event type) — see `app/tools/booking/calcom.py`.
    event_type_id: int | None = None

    def matches(self, text: str) -> bool:
        needle = text.strip().lower()
        if not needle:
            return False
        candidates = {self.slug.lower(), self.name.lower(), *(a.lower() for a in self.aliases)}
        return any(needle == c or needle in c or c in needle for c in candidates)


class EmergencyPolicy(BaseModel):
    """Trade-specific danger patterns plus what to do when one fires."""

    model_config = ConfigDict(frozen=True)

    escalation_phone: str
    keywords: list[str] = Field(default_factory=list)
    holding_message: str = (
        "That sounds like it could be dangerous. Stay safe — I'm getting you to "
        "someone on call right now."
    )
    alert_template: str = (
        "EMERGENCY from {customer_name} ({customer_phone}): {reason}. Caller is on the line."
    )
    #: Kill switch for Vapi warm transfer (Phase 3). Voice escalations still
    #: alert `escalation_phone` by SMS either way; this only controls whether
    #: the assistant is allowed to promise — and attempt — a live handoff.
    allow_warm_transfer: bool = True


class BookingSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: "stub" has no external calendar — it's not fake once the backing
    #: store is Supabase (Phase 4): durable jobs, DB-backed conflict
    #: detection, just no sync to a real calendar. There is deliberately no
    #: separate "supabase" option — that would be the same provider under a
    #: second name, the two-sources-of-truth trap Phase 3 already removed
    #: `booking_provider` for (see CLAUDE.md).
    provider: Literal["stub", "google", "calcom"] = "stub"
    calendar_id: str | None = None
    #: Cal.com only: the tenant-wide event type, normally configured with
    #: multiple durations enabled so one event type serves every service.
    #: A service can override this with its own `Service.event_type_id`.
    event_type_id: int | None = None
    #: Maps our logical field names ("address", "notes") to this tenant's
    #: Cal.com custom booking-field slugs. Empty by default — an unmapped
    #: field is simply omitted from `bookingFieldsResponses` rather than
    #: guessed, since a wrong slug 400s the whole booking.
    booking_field_map: dict[str, str] = Field(default_factory=dict)
    #: Whether `book_job` must collect a street address. Trades that travel to
    #: the customer need it; a hotel booking a guest's own room does not.
    require_address: bool = True
    #: NOTE: for `provider == "calcom"`, `slot_granularity_minutes` and
    #: `lead_time_hours` below are advisory / prompt-only — Cal.com's own
    #: event-type schedule is what actually governs availability. Editing
    #: them will not change what `check_availability` returns for that tenant.
    slot_granularity_minutes: int = Field(default=30, gt=0, le=240)
    lead_time_hours: int = Field(default=2, ge=0, le=24 * 14)
    horizon_days: int = Field(default=10, gt=0, le=90)
    max_slots_returned: int = Field(default=3, gt=0, le=10)


class NotificationSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: Literal["stub", "twilio"] = "stub"
    from_number: str | None = None
    confirmation_template: str = (
        "{business_name}: you're booked for {service} on {when}. "
        "Address: {address}. Reply to this text if anything changes."
    )


class VoiceSettings(BaseModel):
    """How this tenant sounds (Phase 2).

    Deliberately config, never code: changing the voice — or the TTS provider
    entirely — is an edit here plus a re-run of `scripts/provision_vapi.py`.
    Phase 4 voice cloning slots in the same way, since a clone is just another
    `voice_id` on the same provider.
    """

    model_config = ConfigDict(frozen=True)

    provider: Literal["cartesia", "vapi", "11labs", "playht", "deepgram", "azure", "openai"] = (
        "cartesia"
    )
    #: None means "inherit CARTESIA_DEFAULT_VOICE_ID" (resolved at provisioning).
    voice_id: str | None = None
    model: str = "sonic-2"
    speed: float = Field(default=1.0, gt=0.0, le=2.0)


class VapiSettings(BaseModel):
    """Per-tenant Vapi wiring, written back by the provisioning script."""

    model_config = ConfigDict(frozen=True)

    assistant_id: str | None = None
    phone_number_id: str | None = None
    #: Cost guard — caps a runaway test call.
    max_duration_seconds: int = Field(default=600, gt=0, le=43200)
    silence_timeout_seconds: int = Field(default=20, ge=5, le=120)


class ChatSettings(BaseModel):
    """Widget presentation + origin policy (Phase 5).

    Everything here is display-only — it never reaches the graph. The brain
    doesn't know a widget exists; this just tells `/chat/session` what to hand
    back and `/chat` which origins may hold a session token for this tenant.
    """

    model_config = ConfigDict(frozen=True)

    #: Empty = any origin may use this tenant's widget key (the dev default —
    #: see content/README.md). A production tenant should set this.
    allowed_origins: list[str] = Field(default_factory=list)
    accent_color: str = "#0f766e"
    launcher_label: str = "Chat with us"
    quick_replies: bool = True
    #: None falls back to the tenant's own `greeting`.
    greeting: str | None = None


class McpServerConfig(BaseModel):
    """Phase 6 — one MCP server this tenant is allowed to use."""

    model_config = ConfigDict(frozen=True)

    name: str
    transport: Literal["http", "stdio"] = "http"
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    # Secret *references* only — resolved from the vault at load time, never inlined.
    auth_secret_ref: str | None = None


class TenantConfig(BaseModel):
    """Everything the brain needs to serve one business."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    name: str
    trade: str
    timezone: str = "UTC"
    status: Literal["active", "paused", "onboarding"] = "active"

    phone_numbers: list[str] = Field(default_factory=list)
    widget_keys: list[str] = Field(default_factory=list)

    greeting: str
    persona: str = ""
    hours: dict[str, DayHours | None] = Field(default_factory=dict)
    services: list[Service] = Field(default_factory=list)
    emergency: EmergencyPolicy
    booking: BookingSettings = Field(default_factory=BookingSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    vapi: VapiSettings = Field(default_factory=VapiSettings)
    chat: ChatSettings = Field(default_factory=ChatSettings)
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)

    @field_validator("hours")
    @classmethod
    def _known_weekdays(cls, value: dict[str, DayHours | None]) -> dict[str, DayHours | None]:
        unknown = set(value) - set(WEEKDAYS)
        if unknown:
            raise ValueError(f"unknown weekday keys: {sorted(unknown)}")
        return value

    @field_validator("timezone")
    @classmethod
    def _real_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # raises if the tz database doesn't know it
        return value

    @model_validator(mode="after")
    def _unique_service_slugs(self) -> TenantConfig:
        slugs = [s.slug for s in self.services]
        if len(slugs) != len(set(slugs)):
            raise ValueError("service slugs must be unique within a tenant")
        return self

    @model_validator(mode="after")
    def _calcom_tenants_declare_event_types(self) -> TenantConfig:
        """Fail at config load, not mid-call, when a calcom tenant has no way
        to know which event type to book against."""
        if self.booking.provider != "calcom":
            return self
        if self.booking.event_type_id is None and any(
            s.event_type_id is None for s in self.services
        ):
            raise ValueError(
                "booking.provider is 'calcom' but booking.event_type_id is unset and "
                "some services have no event_type_id override — every service needs "
                "either a tenant-wide booking.event_type_id or its own event_type_id"
            )
        return self

    # --- helpers -----------------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def hours_for(self, day: date) -> DayHours | None:
        return self.hours.get(WEEKDAYS[day.weekday()])

    def is_open_at(self, moment: datetime) -> bool:
        hours = self.hours_for(moment.date())
        return bool(hours and hours.open <= moment.timetz().replace(tzinfo=None) < hours.close)

    def find_service(self, text: str) -> Service | None:
        """Best-effort service lookup from whatever the caller said."""
        if not text:
            return None
        for service in self.services:
            if service.slug.lower() == text.strip().lower():
                return service
        for service in self.services:
            if service.matches(text):
                return service
        # Last resort: stem-ish token overlap, so "my lights keep flickering"
        # still finds "lighting-install". The model gets the catalogue back on a
        # miss, so a wrong guess here is recoverable but a silent one isn't.
        tokens = _stems(text)
        best, best_score = None, 0
        for service in self.services:
            haystack = " ".join([service.slug, service.name, *service.aliases])
            score = len(tokens & _stems(haystack))
            if score > best_score:
                best, best_score = service, score
        return best

    def service_by_slug(self, slug: str) -> Service | None:
        return next((s for s in self.services if s.slug == slug), None)

    def hours_summary(self) -> str:
        parts = []
        for day in WEEKDAYS:
            hours = self.hours.get(day)
            label = day[:3].capitalize()
            parts.append(
                f"{label} closed"
                if hours is None
                else f"{label} {hours.open:%H:%M}-{hours.close:%H:%M}"
            )
        return ", ".join(parts)


def _stems(text: str) -> set[str]:
    """Crude 4-char stems — enough to tie "lights" to "lighting" without a stemmer."""
    return {word[:4] for word in re.findall(r"[a-z]{3,}", text.lower())}
