"""Phase 9.4 — opening hours come from whoever owns them.

Before this, a bot's hours lived in tenant config and were quoted from there
even when a real calendar was the thing actually deciding availability. These
tests pin the new ownership rule and the failure modes around it.
"""

from __future__ import annotations

import httpx
import pytest

from app.brain.prompts.system import render_system_prompt
from app.brain.runner import stream_turn
from app.tenancy.models import BookingSettings, DayHours
from app.tools.booking.base import AvailabilitySchedule, BookingProvider, ScheduleWindow
from app.tools.booking.calcom import CalcomBookingProvider
from app.tools.booking.schedule import (
    availability_schedule_for,
    business_hours_for,
)
from app.tools.booking.stub import StubBookingProvider
from app.tools.providers import set_booking_provider
from tests.conftest import ai, mock_http

# The exact payload Cal.com's `GET /v2/schedules` returned for this project's
# own account on 2026-08-17 with `cal-api-version: 2024-06-11`, trimmed only
# of ids. Recorded rather than invented so a shape change shows up as a test
# failure here instead of as missing hours in production.
_CALCOM_SCHEDULES = {
    "status": "success",
    "data": [
        {
            "id": 1678330,
            "ownerId": 2573733,
            "name": "Hotel MZV front desk hours",
            "timeZone": "America/New_York",
            "availability": [
                {
                    "days": [
                        "Sunday",
                        "Monday",
                        "Tuesday",
                        "Wednesday",
                        "Thursday",
                        "Friday",
                        "Saturday",
                    ],
                    "startTime": "07:00",
                    "endTime": "22:00",
                }
            ],
            "isDefault": True,
            "overrides": [],
        }
    ],
}


def _calcom_provider(payload=_CALCOM_SCHEDULES, status=200):
    client, captured = mock_http(lambda _r: httpx.Response(status, json=payload))
    return CalcomBookingProvider(client=client), captured


class TestStubSchedule:
    """The manual grid is authoritative for exactly one provider — the one
    with no calendar behind it. That's the whole reason it still exists."""

    async def test_consecutive_matching_days_collapse(self, northside):
        schedule = await StubBookingProvider().availability_schedule(northside)
        assert schedule is not None
        assert schedule.source == "config"
        assert schedule.summary() == "Mon-Thu 07:00-16:00, Fri 07:00-15:00"

    async def test_a_closed_day_in_the_middle_is_not_swallowed(self, hotel):
        """Mon and Wed share hours with Tuesday shut. They belong in one
        window (same times) but must not read as a Mon-Wed run."""
        tenant = hotel.model_copy(
            update={
                "hours": {
                    "monday": DayHours(open="09:00", close="17:00"),
                    "tuesday": None,
                    "wednesday": DayHours(open="09:00", close="17:00"),
                }
            }
        )
        schedule = await StubBookingProvider().availability_schedule(tenant)
        assert schedule.summary() == "Mon, Wed 09:00-17:00"

    async def test_no_hours_configured_is_none_not_an_empty_string(self, hotel):
        """None means "I can't state hours", which the prompt renders as an
        instruction to check availability. An empty summary would render as a
        blank line the model reads as "no hours"."""
        assert (
            await StubBookingProvider().availability_schedule(
                hotel.model_copy(update={"hours": {}})
            )
            is None
        )

    async def test_the_schedule_carries_the_tenants_timezone(self, northside):
        schedule = await StubBookingProvider().availability_schedule(northside)
        assert schedule.timezone == northside.timezone


class TestCalcomSchedule:
    async def test_maps_the_real_payload(self, hotel):
        provider, captured = _calcom_provider()
        schedule = await provider.availability_schedule(hotel)

        assert schedule.source == "calcom"
        assert schedule.timezone == "America/New_York"
        assert schedule.name == "Hotel MZV front desk hours"
        assert schedule.summary() == "Mon-Sun 07:00-22:00"

    async def test_sends_the_schedules_api_version(self, hotel):
        """Cal.com versions each endpoint family separately; the wrong header
        is a 400 that reads like an auth problem."""
        provider, captured = _calcom_provider()
        await provider.availability_schedule(hotel)

        assert captured[0].url.path.endswith("/schedules")
        assert captured[0].headers["cal-api-version"] == "2024-06-11"

    async def test_prefers_the_default_schedule_over_the_first(self, hotel):
        payload = {
            "data": [
                {
                    "name": "Holiday cover",
                    "timeZone": "UTC",
                    "isDefault": False,
                    "availability": [
                        {"days": ["Sunday"], "startTime": "10:00", "endTime": "12:00"}
                    ],
                },
                {
                    "name": "Normal",
                    "timeZone": "UTC",
                    "isDefault": True,
                    "availability": [
                        {"days": ["Monday"], "startTime": "09:00", "endTime": "17:00"}
                    ],
                },
            ]
        }
        provider, _ = _calcom_provider(payload)
        schedule = await provider.availability_schedule(hotel)
        assert schedule.name == "Normal"

    async def test_no_schedules_is_none(self, hotel):
        provider, _ = _calcom_provider({"data": []})
        assert await provider.availability_schedule(hotel) is None

    async def test_a_schedule_with_no_rules_is_none(self, hotel):
        provider, _ = _calcom_provider({"data": [{"name": "Empty", "availability": []}]})
        assert await provider.availability_schedule(hotel) is None


class TestScheduleShapes:
    """The two Cal.com transports deliver the same fields in different
    wrappers, and `_schedule_from_calcom` is the one mapper for both.

    This shipped broken once: MCP's `get_default_schedule` returns a single
    dict (its envelope already stripped by `_call_tool`) where REST returns a
    list. The mapper only understood the list, so the admin panel showed
    "Cal.com didn't return a schedule" with nothing logged — a silent None,
    not an error.
    """

    _RULES = [{"days": ["Monday"], "startTime": "09:00", "endTime": "17:00"}]

    @pytest.mark.parametrize(
        ("payload", "label"),
        [
            ([{"name": "L", "timeZone": "UTC", "availability": _RULES}], "REST: bare list"),
            (
                {"data": [{"name": "L", "timeZone": "UTC", "availability": _RULES}]},
                "REST: enveloped list",
            ),
            ({"name": "S", "timeZone": "UTC", "availability": _RULES}, "MCP: bare dict"),
            (
                {"data": {"name": "S", "timeZone": "UTC", "availability": _RULES}},
                "MCP: enveloped dict",
            ),
        ],
    )
    def test_every_wrapper_maps(self, payload, label):
        from app.tools.booking.calcom import _schedule_from_calcom

        schedule = _schedule_from_calcom(payload)
        assert schedule is not None, label
        assert schedule.summary() == "Mon 09:00-17:00", label

    @pytest.mark.parametrize("payload", [None, [], {}, {"data": []}, "not json", 42])
    def test_junk_is_none_not_a_crash(self, payload):
        from app.tools.booking.calcom import _schedule_from_calcom

        assert _schedule_from_calcom(payload) is None


class TestCacheAndDegradation:
    """`reason` renders the prompt every single turn, so this lookup sits on
    the latency budget. It must be cached, and it must never raise."""

    async def test_the_second_call_does_not_reach_the_provider(self, northside):
        calls = []

        class Counting(StubBookingProvider):
            async def availability_schedule(self, tenant):
                calls.append(tenant.tenant_id)
                return await super().availability_schedule(tenant)

        set_booking_provider(northside.tenant_id, Counting())
        await availability_schedule_for(northside)
        await availability_schedule_for(northside)
        assert len(calls) == 1

    async def test_a_config_change_invalidates_immediately(self, northside):
        """A TTL alone would leave an admin-panel edit invisible for 15
        minutes — the panel's whole promise is that edits land next turn."""
        calls = []

        class Counting(StubBookingProvider):
            async def availability_schedule(self, tenant):
                calls.append(tenant.tenant_id)
                return await super().availability_schedule(tenant)

        set_booking_provider(northside.tenant_id, Counting())
        await availability_schedule_for(northside)
        edited = northside.model_copy(
            update={"hours": {"monday": DayHours(open="10:00", close="12:00")}}
        )
        schedule = await availability_schedule_for(edited)

        assert len(calls) == 2
        assert schedule.summary() == "Mon 10:00-12:00"

    async def test_a_raising_provider_degrades_to_none(self, hotel):
        class Exploding(StubBookingProvider):
            async def availability_schedule(self, tenant):
                raise RuntimeError("calendar on fire")

        set_booking_provider(hotel.tenant_id, Exploding())
        assert await availability_schedule_for(hotel) is None
        assert await business_hours_for(hotel) is None

    async def test_a_failure_is_cached_too(self, hotel):
        """A calendar that is down stays down for more than one turn; retrying
        every turn would add a timeout to each one."""
        calls = []

        class Exploding(StubBookingProvider):
            async def availability_schedule(self, tenant):
                calls.append(1)
                raise RuntimeError("calendar on fire")

        set_booking_provider(hotel.tenant_id, Exploding())
        await availability_schedule_for(hotel)
        await availability_schedule_for(hotel)
        assert len(calls) == 1

    async def test_refresh_bypasses_the_cache(self, northside):
        calls = []

        class Counting(StubBookingProvider):
            async def availability_schedule(self, tenant):
                calls.append(1)
                return await super().availability_schedule(tenant)

        set_booking_provider(northside.tenant_id, Counting())
        await availability_schedule_for(northside)
        await availability_schedule_for(northside, refresh=True)
        assert len(calls) == 2

    async def test_a_bare_provider_has_no_schedule(self, hotel):
        """`availability_schedule` is deliberately non-abstract, so a provider
        that never heard of it keeps working."""

        class Minimal(BookingProvider):
            async def check_availability(self, *a, **k):
                return []

            async def create_booking(self, *a, **k): ...
            async def cancel(self, *a, **k): ...
            async def reschedule(self, *a, **k): ...

        set_booking_provider(hotel.tenant_id, Minimal())
        assert await availability_schedule_for(hotel) is None


class TestPromptLine:
    """Which source wins, and — the part that matters — which one must not."""

    def test_a_live_value_beats_the_local_grid(self, northside):
        prompt = render_system_prompt(
            northside, channel="chat", business_hours="Mon-Fri 08:00-20:00"
        )
        assert "Business hours: Mon-Fri 08:00-20:00" in prompt
        assert "07:00-16:00" not in prompt

    def test_a_stub_tenant_still_falls_back_to_its_grid(self, northside):
        """The guard that makes this phase safe to ship: a bot with no
        calendar behaves exactly as it did before."""
        prompt = render_system_prompt(northside, channel="chat")
        assert f"Business hours: {northside.hours_summary()}" in prompt

    def test_a_calcom_tenant_never_quotes_its_stale_grid(self, hotel):
        """The grid is hidden in the panel once Cal.com owns availability, so
        it's whatever was typed before the calendar took over. Reciting it
        would be confidently quoting something nobody maintains."""
        tenant = hotel.model_copy(
            update={
                "booking": BookingSettings(provider="calcom", event_type_id=1),
                "hours": {"monday": DayHours(open="01:00", close="02:00")},
            }
        )
        prompt = render_system_prompt(tenant, channel="chat")
        assert "01:00-02:00" not in prompt
        assert "check_availability" in prompt

    def test_no_hours_anywhere_never_claims_the_business_is_shut(self, hotel):
        """The bug this replaces: an empty grid rendered as "Mon closed, Tue
        closed, …" and the bot told callers it never opens."""
        prompt = render_system_prompt(hotel.model_copy(update={"hours": {}}), channel="chat")
        assert "Mon closed" not in prompt
        assert "Business hours: not listed here" in prompt


class TestScheduleFormatting:
    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], "Mon-Fri"),
            (["Saturday", "Sunday"], "Sat-Sun"),
            (["Monday"], "Mon"),
            (["Monday", "Wednesday", "Friday"], "Mon, Wed, Fri"),
            # Cal.com lists Sunday first; the compression must not depend on
            # the order the provider happened to use.
            (["Sunday", "Monday", "Tuesday"], "Mon-Tue, Sun"),
            (["Nonsense"], ""),
        ],
    )
    def test_day_runs_compress(self, days, expected):
        window = ScheduleWindow(days=days, start="09:00", end="17:00")
        # An unrecognised weekday must not leave a leading space behind.
        assert window.label() == (f"{expected} 09:00-17:00" if expected else "09:00-17:00")

    def test_multiple_windows_join(self):
        schedule = AvailabilitySchedule(
            windows=[
                ScheduleWindow(days=["Monday", "Tuesday"], start="09:00", end="17:00"),
                ScheduleWindow(days=["Saturday"], start="10:00", end="14:00"),
            ]
        )
        assert schedule.summary() == "Mon-Tue 09:00-17:00, Sat 10:00-14:00"


async def test_a_real_turn_renders_the_providers_hours(scripted, northside):
    """The whole path, through the graph: provider -> cache -> prompt."""

    class Fixed(StubBookingProvider):
        async def availability_schedule(self, tenant):
            return AvailabilitySchedule(
                windows=[ScheduleWindow(days=["Monday"], start="11:00", end="13:00")],
                source="calcom",
            )

    set_booking_provider(northside.tenant_id, Fixed())
    model = scripted(ai("Hello."))
    async for _ in stream_turn(
        text="when are you open?", tenant_id=northside.tenant_id, session_id="hours"
    ):
        pass

    system_prompt = str(model.seen_prompts[0][0].content)
    assert "Business hours: Mon 11:00-13:00" in system_prompt


def test_the_schedule_type_is_json_safe():
    """It crosses the wire to the admin panel, so it has to survive a dump."""
    schedule = AvailabilitySchedule(
        windows=[ScheduleWindow(days=["Monday"], start="09:00", end="17:00")],
        timezone="Asia/Karachi",
        source="calcom",
    )
    dumped = schedule.model_dump(mode="json")
    assert dumped["windows"][0]["days"] == ["Monday"]
    assert dumped["source"] == "calcom"
