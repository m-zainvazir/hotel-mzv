"""Tool-level Cal.com contract: dispatch, the dead-call guard, and the
per-tenant require_address switch (app/tools/booking_tools.py).

Tenant overrides go through the `override_tenant` fixture (conftest.py), not
a bare `hotel.model_copy(...)` — tools re-resolve their tenant via
`get_tenant_config(tenant_id)`, which reads the cached repository and would
otherwise never see a locally modified object.
"""

from __future__ import annotations

import re

from app.tools.booking.base import BookingError
from app.tools.booking.calcom import CalcomBookingProvider
from app.tools.booking_tools import book_job, check_availability
from app.tools.providers import get_booking_provider, reset_provider_overrides, set_booking_provider
from tests.conftest import tool_config


def _hotel_calcom(hotel, *, require_address: bool = True):
    booking = hotel.booking.model_copy(
        update={
            "provider": "calcom",
            "event_type_id": 1234,
            "require_address": require_address,
        }
    )
    return hotel.model_copy(update={"booking": booking})


class _AlwaysFails(CalcomBookingProvider):
    async def check_availability(self, *args, **kwargs):
        raise BookingError("the calendar is not responding")

    async def create_booking(self, *args, **kwargs):
        raise BookingError("the calendar is not responding")


async def test_get_booking_provider_dispatches_calcom(hotel, override_tenant):
    tenant = _hotel_calcom(hotel)
    override_tenant(tenant)
    # conftest.isolated_runtime pins every real tenant id to the stub as a
    # test-hermeticity safety net; this test's whole point is to check the
    # *natural* dispatch logic, so clear that pin for this one tenant first.
    set_booking_provider(tenant.tenant_id, None)
    provider = get_booking_provider(tenant)
    assert isinstance(provider, CalcomBookingProvider)


async def test_check_availability_booking_error_becomes_error_string_not_exception(
    hotel, override_tenant
):
    tenant = _hotel_calcom(hotel)
    override_tenant(tenant)
    set_booking_provider(tenant.tenant_id, _AlwaysFails())
    try:
        result = await check_availability.ainvoke(
            {"service": "spa-treatment"}, config=tool_config(tenant.tenant_id)
        )
    finally:
        reset_provider_overrides()

    assert isinstance(result, str)
    assert result.startswith("ERROR:")


async def test_book_job_booking_error_becomes_error_string_and_never_claims_booked(
    hotel, override_tenant
):
    tenant = _hotel_calcom(hotel)
    override_tenant(tenant)
    set_booking_provider(tenant.tenant_id, _AlwaysFails())
    try:
        result = await book_job.ainvoke(
            {
                "service": "spa-treatment",
                "slot_start_iso": "2050-09-05T09:00:00-04:00",
                "customer_name": "Jane Doe",
                "customer_phone": "+15551234567",
                "address": "123 Main St",
            },
            config=tool_config(tenant.tenant_id),
        )
    finally:
        reset_provider_overrides()

    assert isinstance(result, str)
    assert result.startswith("ERROR:")
    assert "Do NOT say it is booked" in result
    assert "BOOKED" not in result


async def test_require_address_true_still_rejects_missing_address(hotel, override_tenant):
    tenant = _hotel_calcom(hotel, require_address=True)
    override_tenant(tenant)

    result = await book_job.ainvoke(
        {
            "service": "spa-treatment",
            "slot_start_iso": "2050-09-05T09:00:00-04:00",
            "customer_name": "Jane Doe",
            "customer_phone": "+15551234567",
            "address": "",
        },
        config=tool_config(tenant.tenant_id),
    )
    assert result.startswith("ERROR: address is required")


async def test_require_address_false_books_with_no_address(hotel, override_tenant):
    # Stays on the stub provider (require_address is provider-independent).
    booking = hotel.booking.model_copy(update={"require_address": False})
    tenant = hotel.model_copy(update={"booking": booking})
    override_tenant(tenant)

    listing = await check_availability.ainvoke(
        {"service": "spa-treatment"}, config=tool_config(tenant.tenant_id)
    )
    slot_iso = listing.split("slot_start_iso=")[1].split(")")[0]

    result = await book_job.ainvoke(
        {
            "service": "spa-treatment",
            "slot_start_iso": slot_iso,
            "customer_name": "Jane Doe",
            "customer_phone": "+15551234567",
            "address": "",
        },
        config=tool_config(tenant.tenant_id),
    )
    assert result.startswith("BOOKED")
    # No address suffix: the time phrase is immediately followed by ". Now call…"
    # rather than " at <address>.".
    assert ". Now call send_confirmation" in result
    assert re.search(r"(am|pm) at ", result) is None
