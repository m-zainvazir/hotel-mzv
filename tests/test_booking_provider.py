"""Stub booking provider — real scheduling rules, fake calendar."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db.memory_store import get_store
from app.tools.booking.base import BookingRequest, SlotUnavailableError
from app.tools.booking.stub import StubBookingProvider


@pytest.fixture
def provider():
    return StubBookingProvider()


async def test_slots_respect_hours_lead_time_and_limit(provider, hotel):
    service = hotel.service_by_slug("room-reservation")
    slots = await provider.check_availability(hotel, service)

    assert len(slots) == hotel.booking.max_slots_returned
    now = datetime.now(hotel.tz)
    for slot in slots:
        assert slot.start >= now + timedelta(hours=hotel.booking.lead_time_hours)
        assert hotel.is_open_at(slot.start)
        hours = hotel.hours_for(slot.start.date())
        assert slot.end.timetz().replace(tzinfo=None) <= hours.close
        assert slot.end - slot.start == timedelta(minutes=service.duration_minutes)


async def test_slots_are_sorted_and_distinct(provider, hotel):
    slots = await provider.check_availability(hotel, hotel.service_by_slug("room-reservation"))
    starts = [s.start for s in slots]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)


async def test_long_service_never_overruns_closing_time(provider, hotel):
    """A 4h event booking can't start at 7pm on a day that closes at 10pm."""
    slots = await provider.check_availability(hotel, hotel.service_by_slug("event-space"))
    for slot in slots:
        hours = hotel.hours_for(slot.start.date())
        assert slot.end.timetz().replace(tzinfo=None) <= hours.close


async def test_earliest_constraint_is_honoured(provider, hotel):
    service = hotel.service_by_slug("room-reservation")
    floor = datetime.now(hotel.tz) + timedelta(days=3)
    slots = await provider.check_availability(hotel, service, earliest=floor)
    assert slots and all(s.start >= floor for s in slots)


async def test_booking_writes_the_authoritative_job_row(provider, hotel):
    service = hotel.service_by_slug("room-reservation")
    slot = (await provider.check_availability(hotel, service))[0]

    job = await provider.create_booking(
        hotel,
        BookingRequest(
            service_slug=service.slug,
            start=slot.start,
            customer_name="Dana Reyes",
            customer_phone="+15551112222",
            address="12 Oak Street",
        ),
    )

    assert job.tenant_id == hotel.tenant_id
    assert job.scheduled_end - job.scheduled_start == timedelta(minutes=service.duration_minutes)
    assert job.calendar_event_id  # links to whatever calendar the provider used
    assert get_store().get(hotel.tenant_id, job.id) == job


async def test_booked_slot_disappears_from_availability(provider, hotel):
    service = hotel.service_by_slug("room-reservation")
    first = (await provider.check_availability(hotel, service))[0]

    await provider.create_booking(
        hotel,
        BookingRequest(
            service_slug=service.slug,
            start=first.start,
            customer_name="Dana",
            customer_phone="+15551112222",
            address="12 Oak Street",
        ),
    )

    after = await provider.check_availability(hotel, service)
    assert all(slot.start != first.start for slot in after)


async def test_double_booking_is_refused(provider, hotel):
    service = hotel.service_by_slug("room-reservation")
    slot = (await provider.check_availability(hotel, service))[0]
    request = BookingRequest(
        service_slug=service.slug,
        start=slot.start,
        customer_name="Dana",
        customer_phone="+15551112222",
        address="12 Oak Street",
    )
    await provider.create_booking(hotel, request)

    with pytest.raises(SlotUnavailableError):
        await provider.create_booking(hotel, request)


async def test_booking_outside_hours_is_refused(provider, hotel):
    service = hotel.service_by_slug("room-reservation")
    midnight = (datetime.now(hotel.tz) + timedelta(days=1)).replace(
        hour=2, minute=0, second=0, microsecond=0
    )
    with pytest.raises(SlotUnavailableError):
        await provider.create_booking(
            hotel,
            BookingRequest(
                service_slug=service.slug,
                start=midnight,
                customer_name="Dana",
                customer_phone="+15551112222",
                address="12 Oak Street",
            ),
        )


async def test_cancel_frees_the_slot(provider, hotel):
    service = hotel.service_by_slug("room-reservation")
    slot = (await provider.check_availability(hotel, service))[0]
    job = await provider.create_booking(
        hotel,
        BookingRequest(
            service_slug=service.slug,
            start=slot.start,
            customer_name="Dana",
            customer_phone="+15551112222",
            address="12 Oak Street",
        ),
    )

    await provider.cancel(hotel, job.id)
    after = await provider.check_availability(hotel, service)
    assert any(s.start == slot.start for s in after)


async def test_provider_cannot_touch_another_tenants_job(provider, hotel, northside):
    service = hotel.service_by_slug("room-reservation")
    slot = (await provider.check_availability(hotel, service))[0]
    job = await provider.create_booking(
        hotel,
        BookingRequest(
            service_slug=service.slug,
            start=slot.start,
            customer_name="Dana",
            customer_phone="+15551112222",
            address="12 Oak Street",
        ),
    )

    with pytest.raises(SlotUnavailableError):
        await provider.cancel(northside, job.id)
