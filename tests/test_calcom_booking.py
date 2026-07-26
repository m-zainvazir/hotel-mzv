"""CalcomBookingProvider: request shape, response mapping, and error handling.

No network — every client is built with httpx.MockTransport via mock_http().
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import Settings
from app.db.memory_store import get_store
from app.tools.booking.base import BookingError, BookingRequest, SlotUnavailableError
from app.tools.booking.calcom import CalcomBookingProvider
from tests.conftest import mock_http

_AUTH_HEADERS = {"Authorization": "Bearer cal_test_key"}


def _mock_calcom(handler):
    """mock_http, but pre-loaded with the Authorization header a real
    settings-built client would carry (an injected client otherwise has
    none — see app/tools/booking/calcom.py::_get_client)."""
    return mock_http(handler, headers=_AUTH_HEADERS)


def _settings(**overrides) -> Settings:
    base = {"calcom_api_key": "cal_test_key"}
    base.update(overrides)
    return Settings(**base)


def _hotel_calcom(hotel, **booking_overrides):
    booking = hotel.booking.model_copy(
        update={"provider": "calcom", "event_type_id": 1234, **booking_overrides}
    )
    return hotel.model_copy(update={"booking": booking})


def _slots_response(*starts: str) -> httpx.Response:
    data = {}
    for start in starts:
        day = start[:10]
        end = (datetime.fromisoformat(start) + timedelta(minutes=30)).isoformat()
        data.setdefault(day, []).append({"start": start, "end": end})
    return httpx.Response(200, json={"status": "success", "data": data})


class TestCheckAvailability:
    async def test_request_shape(self, hotel):
        tenant = _hotel_calcom(hotel)
        service = tenant.service_by_slug("spa-treatment")

        def handler(_req: httpx.Request) -> httpx.Response:
            return _slots_response()

        client, requests = _mock_calcom(handler)
        provider = CalcomBookingProvider(client=client, settings=_settings())

        await provider.check_availability(tenant, service)

        assert len(requests) == 1
        request = requests[0]
        assert request.url.path == "/slots"
        assert request.headers["cal-api-version"] == "2024-09-04"
        assert request.headers["authorization"] == "Bearer cal_test_key"
        params = dict(request.url.params)
        assert params["eventTypeId"] == "1234"
        assert params["timeZone"] == tenant.timezone
        assert params["format"] == "range"
        # shared (multi-duration) event type: duration must be sent
        assert params["duration"] == str(service.duration_minutes)

    async def test_per_service_event_type_omits_duration(self, hotel):
        tenant = _hotel_calcom(hotel)
        service = tenant.service_by_slug("spa-treatment").model_copy(update={"event_type_id": 9999})
        tenant = tenant.model_copy(
            update={"services": [s if s.slug != service.slug else service for s in tenant.services]}
        )

        def handler(_req: httpx.Request) -> httpx.Response:
            return _slots_response()

        client, requests = _mock_calcom(handler)
        provider = CalcomBookingProvider(client=client, settings=_settings())

        await provider.check_availability(tenant, service)

        params = dict(requests[0].url.params)
        assert params["eventTypeId"] == "9999"
        assert "duration" not in params

    async def test_flattens_sorts_and_converts_timezone(self, hotel):
        tenant = _hotel_calcom(hotel)
        service = tenant.service_by_slug("spa-treatment")

        def handler(_req: httpx.Request) -> httpx.Response:
            return _slots_response(
                "2050-09-06T09:00:00.000+00:00",
                "2050-09-05T09:00:00.000+00:00",
            )

        client, _requests = _mock_calcom(handler)
        provider = CalcomBookingProvider(client=client, settings=_settings())

        slots = await provider.check_availability(tenant, service)

        assert len(slots) == 2
        assert slots[0].start < slots[1].start
        assert slots[0].start.tzinfo is not None

    async def test_max_slots_returned_truncates(self, hotel):
        tenant = _hotel_calcom(hotel, max_slots_returned=1)
        service = tenant.service_by_slug("spa-treatment")

        def handler(_req: httpx.Request) -> httpx.Response:
            return _slots_response("2050-09-05T09:00:00.000+00:00", "2050-09-05T10:00:00.000+00:00")

        client, _requests = _mock_calcom(handler)
        provider = CalcomBookingProvider(client=client, settings=_settings())

        slots = await provider.check_availability(tenant, service)
        assert len(slots) == 1

    async def test_empty_data_returns_empty_list(self, hotel):
        tenant = _hotel_calcom(hotel)
        service = tenant.service_by_slug("spa-treatment")

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "data": {}})

        client, _requests = _mock_calcom(handler)
        provider = CalcomBookingProvider(client=client, settings=_settings())

        assert await provider.check_availability(tenant, service) == []

    async def test_earliest_moves_the_window(self, hotel):
        tenant = _hotel_calcom(hotel)
        service = tenant.service_by_slug("spa-treatment")
        earliest = datetime(2050, 9, 10, tzinfo=UTC)

        def handler(_req: httpx.Request) -> httpx.Response:
            return _slots_response()

        client, requests = _mock_calcom(handler)
        provider = CalcomBookingProvider(client=client, settings=_settings())

        await provider.check_availability(tenant, service, earliest=earliest)

        params = dict(requests[0].url.params)
        sent_start = datetime.fromisoformat(params["start"])
        assert sent_start == earliest.astimezone(tenant.tz)

    async def test_missing_event_type_raises_with_zero_http_calls(self, hotel):
        tenant = _hotel_calcom(hotel, event_type_id=None)
        service = tenant.service_by_slug("spa-treatment")

        client, requests = _mock_calcom(lambda req: _slots_response())
        provider = CalcomBookingProvider(client=client, settings=_settings())

        with pytest.raises(BookingError):
            await provider.check_availability(tenant, service)
        assert requests == []

    async def test_missing_api_key_raises_with_zero_http_calls(self, hotel):
        """No client injected here: the point is that the provider must refuse
        to build one at all when unconfigured — an injected client would
        bypass that check by construction, not exercise it."""
        tenant = _hotel_calcom(hotel)
        service = tenant.service_by_slug("spa-treatment")

        provider = CalcomBookingProvider(settings=_settings(calcom_api_key=None))

        with pytest.raises(BookingError):
            await provider.check_availability(tenant, service)


class TestCreateBooking:
    def _request(self, tenant, **overrides) -> BookingRequest:
        base = dict(
            service_slug="spa-treatment",
            start=datetime(2050, 9, 5, 9, 0, tzinfo=tenant.tz),
            customer_name="Jane Doe",
            customer_phone="+15551234567",
            address="123 Main St",
        )
        base.update(overrides)
        return BookingRequest(**base)

    def _booked_response(self, **overrides) -> httpx.Response:
        data = {
            "id": 1,
            "uid": "cal_uid_abc",
            "status": "accepted",
            "start": "2050-09-05T13:00:00.000Z",
            "end": "2050-09-05T14:00:00.000Z",
        }
        data.update(overrides)
        return httpx.Response(201, json={"status": "success", "data": data})

    async def test_request_shape_and_uid_stored(self, hotel):
        tenant = _hotel_calcom(hotel)
        client, requests = _mock_calcom(lambda req: self._booked_response())
        provider = CalcomBookingProvider(client=client, settings=_settings())

        job = await provider.create_booking(tenant, self._request(tenant))

        assert len(requests) == 1
        request = requests[0]
        assert request.url.path == "/bookings"
        assert request.headers["cal-api-version"] == "2024-08-13"

        payload = json.loads(request.content)
        assert payload["eventTypeId"] == 1234
        assert payload["start"].endswith("Z")
        assert payload["attendee"]["name"] == "Jane Doe"
        assert payload["attendee"]["email"]
        assert payload["lengthInMinutes"] == 60
        assert payload["metadata"]["job_id"] == job.id

        assert job.calendar_event_id == "cal_uid_abc"
        # Local store row is authoritative and was written.
        assert get_store().get(tenant.tenant_id, job.id) is not None

    async def test_deterministic_placeholder_email_when_none_supplied(self, hotel):
        tenant = _hotel_calcom(hotel)
        client, requests = _mock_calcom(lambda req: self._booked_response())
        provider = CalcomBookingProvider(client=client, settings=_settings())

        await provider.create_booking(tenant, self._request(tenant, customer_phone="+15551234567"))
        await provider.create_booking(
            tenant,
            self._request(
                tenant,
                customer_phone="+15551234567",
                start=datetime(2050, 9, 6, 9, tzinfo=tenant.tz),
            ),
        )

        email_1 = json.loads(requests[0].content)["attendee"]["email"]
        email_2 = json.loads(requests[1].content)["attendee"]["email"]
        assert email_1 == email_2
        assert email_1.startswith("caller-15551234567@")

    async def test_supplied_email_takes_precedence(self, hotel):
        tenant = _hotel_calcom(hotel)
        client, requests = _mock_calcom(lambda req: self._booked_response())
        provider = CalcomBookingProvider(client=client, settings=_settings())

        await provider.create_booking(
            tenant, self._request(tenant, customer_email="jane@example.com")
        )

        assert json.loads(requests[0].content)["attendee"]["email"] == "jane@example.com"

    async def test_job_times_taken_from_api_response_not_arithmetic(self, hotel):
        tenant = _hotel_calcom(hotel)
        # API disagrees with our computed end time (e.g. lengthInMinutes ignored).
        client, _requests = _mock_calcom(
            lambda req: self._booked_response(
                start="2050-09-05T13:00:00.000Z", end="2050-09-05T13:30:00.000Z"
            )
        )
        provider = CalcomBookingProvider(client=client, settings=_settings())

        job = await provider.create_booking(tenant, self._request(tenant))

        assert (job.scheduled_end - job.scheduled_start) == timedelta(minutes=30)

    async def test_naive_datetime_is_refused(self, hotel):
        tenant = _hotel_calcom(hotel)
        client, requests = _mock_calcom(lambda req: self._booked_response())
        provider = CalcomBookingProvider(client=client, settings=_settings())

        naive_request = self._request(tenant).model_copy(
            update={"start": datetime(2050, 9, 5, 9, 0)}
        )
        with pytest.raises(BookingError):
            await provider.create_booking(tenant, naive_request)
        assert requests == []

    @pytest.mark.parametrize(
        "status_code,body,expected",
        [
            (409, {"error": {"message": "slot already booked"}}, SlotUnavailableError),
            (400, {"error": {"message": "no longer available"}}, SlotUnavailableError),
            (401, {"error": {"message": "bad key"}}, BookingError),
            (404, {"error": {"message": "not found"}}, BookingError),
            (500, {"error": {"message": "boom"}}, BookingError),
        ],
    )
    async def test_error_mapping(self, hotel, status_code, body, expected):
        tenant = _hotel_calcom(hotel)
        client, _requests = _mock_calcom(lambda req: httpx.Response(status_code, json=body))
        provider = CalcomBookingProvider(client=client, settings=_settings())

        with pytest.raises(expected):
            await provider.create_booking(tenant, self._request(tenant))

    async def test_timeout_raises_booking_error(self, hotel):
        tenant = _hotel_calcom(hotel)

        def handler(_req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

        client, _requests = _mock_calcom(handler)
        provider = CalcomBookingProvider(client=client, settings=_settings())

        with pytest.raises(BookingError):
            await provider.create_booking(tenant, self._request(tenant))

    async def test_raw_provider_text_never_leaks_into_the_exception_message(self, hotel):
        tenant = _hotel_calcom(hotel)
        secret_detail = "internal-stack-trace-xyz"
        client, _requests = _mock_calcom(
            lambda req: httpx.Response(500, json={"error": {"message": secret_detail}})
        )
        provider = CalcomBookingProvider(client=client, settings=_settings())

        with pytest.raises(BookingError) as exc_info:
            await provider.create_booking(tenant, self._request(tenant))
        assert secret_detail not in str(exc_info.value)
