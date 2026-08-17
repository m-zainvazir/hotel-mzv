"""McpBookingProvider: tool-call shapes, error mapping, session caching
(Phase 9 Part A).

No network — a fake MCP session (matching `mcp.ClientSession.call_tool`'s
shape closely enough for this provider's needs) is injected directly via
`session=`, or a fake `connector` is injected to exercise the connect-time
caching and failure paths. See `app/tools/booking/mcp_calcom.py`'s module
docstring for why the tool argument shapes asserted below are a best-effort
guess, not a verified contract — `plans/phase9.md` live check 3 is what
actually confirms them.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.config import Settings
from app.db.memory_store import get_store
from app.tools.booking.base import BookingError, BookingRequest, SlotUnavailableError
from app.tools.booking.mcp_calcom import McpBookingProvider, aclose_calcom_mcp_sessions


def _hotel_mcp(hotel, **booking_overrides):
    booking = hotel.booking.model_copy(
        update={"provider": "mcp_calcom", "event_type_id": 1234, **booking_overrides}
    )
    return hotel.model_copy(update={"booking": booking})


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


@dataclass
class _FakeResult:
    structuredContent: dict[str, Any] | None = None
    content: list[Any] = field(default_factory=list)
    isError: bool = False


class _FakeSession:
    def __init__(self, *results: _FakeResult):
        self._results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _FakeResult:
        self.calls.append((name, arguments))
        return self._results.pop(0)


class _DeadSession:
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _FakeResult:
        raise ConnectionError("dead server")


class _CancelledSession:
    """The mcp SDK's SSE read timeout fires via an internal cancel scope, not
    a clean TimeoutError — observed live (2026-08-03) as a bare
    asyncio.CancelledError out of session.call_tool. CancelledError doesn't
    subclass Exception (Python 3.8+), so it needs its own except clause."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _FakeResult:
        raise asyncio.CancelledError()


@dataclass
class _TextResult:
    """A tool result shaped as a plain text content block instead of
    structuredContent — the fallback JSON-parsing path in
    `_extract_payload`."""

    text: str
    isError: bool = False

    @property
    def content(self) -> list[Any]:
        return [type("Block", (), {"text": self.text})()]

    structuredContent: None = None


def _ok(payload: dict[str, Any]) -> _FakeResult:
    # Wrapped in Cal.com's own REST-mirrored envelope — confirmed live
    # (2026-08-03) this is really how the hosted MCP server shapes every
    # dict result, `data` one level deeper than callers expect at first
    # glance. `_unwrap_envelope` (app/tools/booking/mcp_calcom.py) is what
    # strips it; this fixture must stay wrapped or the tests would exercise
    # an unrealistic shape the live server never actually sends.
    return _FakeResult(structuredContent={"status": "success", "data": payload})


def _error(message: str) -> _FakeResult:
    return _FakeResult(structuredContent={"message": message}, isError=True)


class TestCheckAvailability:
    async def test_request_shape(self, hotel):
        tenant = _hotel_mcp(hotel)
        service = tenant.service_by_slug("spa-treatment")
        session = _FakeSession(_ok({}))
        provider = McpBookingProvider(session=session, settings=_settings())

        await provider.check_availability(tenant, service)

        assert len(session.calls) == 1
        name, arguments = session.calls[0]
        assert name == "get_availability"
        assert arguments["eventTypeId"] == 1234
        assert arguments["timeZone"] == tenant.timezone
        assert arguments["duration"] == service.duration_minutes

    async def test_per_service_event_type_omits_duration(self, hotel):
        tenant = _hotel_mcp(hotel)
        service = tenant.service_by_slug("spa-treatment").model_copy(update={"event_type_id": 9999})
        tenant = tenant.model_copy(
            update={"services": [s if s.slug != service.slug else service for s in tenant.services]}
        )
        session = _FakeSession(_ok({}))
        provider = McpBookingProvider(session=session, settings=_settings())

        await provider.check_availability(tenant, service)

        _name, arguments = session.calls[0]
        assert arguments["eventTypeId"] == 9999
        assert "duration" not in arguments

    async def test_flattens_sorts_and_converts_timezone(self, hotel):
        tenant = _hotel_mcp(hotel)
        service = tenant.service_by_slug("spa-treatment")
        payload = {
            "2050-09-06": [
                {"start": "2050-09-06T09:00:00.000+00:00", "end": "2050-09-06T09:30:00.000+00:00"}
            ],
            "2050-09-05": [
                {"start": "2050-09-05T09:00:00.000+00:00", "end": "2050-09-05T09:30:00.000+00:00"}
            ],
        }
        session = _FakeSession(_ok(payload))
        provider = McpBookingProvider(session=session, settings=_settings())

        slots = await provider.check_availability(tenant, service)

        assert len(slots) == 2
        assert slots[0].start < slots[1].start
        assert slots[0].start.tzinfo is not None

    async def test_flat_list_response_shape_is_also_handled(self, hotel):
        tenant = _hotel_mcp(hotel)
        service = tenant.service_by_slug("spa-treatment")
        payload = [
            {"start": "2050-09-05T09:00:00.000+00:00", "end": "2050-09-05T09:30:00.000+00:00"}
        ]
        session = _FakeSession(_ok(payload))
        provider = McpBookingProvider(session=session, settings=_settings())

        slots = await provider.check_availability(tenant, service)
        assert len(slots) == 1

    async def test_text_content_block_is_parsed_as_json(self, hotel):
        tenant = _hotel_mcp(hotel)
        service = tenant.service_by_slug("spa-treatment")
        session = _FakeSession(
            _TextResult(
                text='{"2050-09-05": [{"start": "2050-09-05T09:00:00.000+00:00", '
                '"end": "2050-09-05T09:30:00.000+00:00"}]}'
            )
        )
        provider = McpBookingProvider(session=session, settings=_settings())

        slots = await provider.check_availability(tenant, service)
        assert len(slots) == 1

    async def test_missing_event_type_raises_with_zero_tool_calls(self, hotel):
        tenant = _hotel_mcp(hotel, event_type_id=None)
        service = tenant.service_by_slug("spa-treatment")
        session = _FakeSession()
        provider = McpBookingProvider(session=session, settings=_settings())

        with pytest.raises(BookingError):
            await provider.check_availability(tenant, service)
        assert session.calls == []


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

    def _booked(self, **overrides) -> _FakeResult:
        data = {
            "id": 1,
            "uid": "cal_uid_abc",
            "status": "accepted",
            "start": "2050-09-05T13:00:00.000Z",
            "end": "2050-09-05T14:00:00.000Z",
        }
        data.update(overrides)
        return _ok(data)

    async def test_request_shape_local_job_first_and_uid_stored(self, hotel):
        tenant = _hotel_mcp(hotel)
        session = _FakeSession(self._booked())
        provider = McpBookingProvider(session=session, settings=_settings())

        job = await provider.create_booking(tenant, self._request(tenant))

        assert len(session.calls) == 1
        name, arguments = session.calls[0]
        assert name == "create_booking"
        assert arguments["eventTypeId"] == 1234
        assert arguments["start"].endswith("Z")
        assert arguments["attendee"]["name"] == "Jane Doe"
        assert arguments["attendee"]["email"]
        assert arguments["lengthInMinutes"] == 60
        # The local Job is built BEFORE the tool call — its id is what
        # `metadata.job_id` carries as the reconciliation handle.
        assert arguments["metadata"]["job_id"] == job.id

        assert job.calendar_event_id == "cal_uid_abc"
        # Local store row stays authoritative — send_confirmation looks it up.
        assert get_store().get(tenant.tenant_id, job.id) is not None

    async def test_retries_without_lengthinminutes_on_fixed_length_event_type(self, hotel):
        """Mirrors CalcomBookingProvider's own live-verified quirk: Cal.com
        400s the *entire* booking, even a lengthInMinutes that exactly
        matches the event type's own length, whenever that event type has
        no multiple-duration option enabled — found live against the MCP
        provider (2026-08-03), which had never ported the REST provider's
        retry."""
        tenant = _hotel_mcp(hotel)
        session = _FakeSession(
            _error(
                "Can't specify 'lengthInMinutes' because event type does not "
                "have multiple possible lengths."
            ),
            self._booked(),
        )
        provider = McpBookingProvider(session=session, settings=_settings())

        job = await provider.create_booking(tenant, self._request(tenant))

        assert len(session.calls) == 2
        first_args, second_args = session.calls[0][1], session.calls[1][1]
        assert "lengthInMinutes" in first_args
        assert "lengthInMinutes" not in second_args
        assert job.calendar_event_id == "cal_uid_abc"

    async def test_real_envelope_shape_observed_live_is_unwrapped_correctly(self, hotel):
        """The exact response shape captured live (2026-08-03) via a direct
        diagnostic probe against Cal.com's real hosted MCP server: delivered
        as a JSON-encoded TEXT block (structuredContent was null), wrapped in
        `{"status": "success", "data": {...}}` — the bug this whole test
        class exists to catch: `create_booking` used to read `uid`/`id` off
        the OUTER envelope, which doesn't have them, and treated a real,
        successful booking as a failure."""
        tenant = _hotel_mcp(hotel)
        raw_text = (
            '{"status":"success","data":{"id":23302122,"uid":"1BpGuzZf4AaJYRds6YHrEF",'
            '"status":"accepted","start":"2026-08-13T15:00:00.000Z",'
            '"end":"2026-08-13T15:30:00.000Z","duration":30,"eventTypeId":6446177}}'
        )
        session = _FakeSession(_TextResult(text=raw_text))
        provider = McpBookingProvider(session=session, settings=_settings())

        job = await provider.create_booking(tenant, self._request(tenant))

        assert job.calendar_event_id == "1BpGuzZf4AaJYRds6YHrEF"

    async def test_a_response_with_no_uid_or_id_is_a_failure_not_a_phantom_booking(self, hotel):
        """Found live (2026-08-03): a nonexistent event_type_id produced a
        non-error MCP response with no uid/id at all — this provider used to
        trust it as a success, saving a local job with calendar_event_id=None
        and telling the caller it was booked for something Cal.com never
        created. A missing uid/id must fail loudly instead."""
        tenant = _hotel_mcp(hotel)
        session = _FakeSession(_ok({}))
        provider = McpBookingProvider(session=session, settings=_settings())

        with pytest.raises(BookingError):
            await provider.create_booking(tenant, self._request(tenant))

        # Nothing should have been recorded as a real booking.
        assert get_store().list_jobs(tenant.tenant_id) == []

    async def test_an_unrelated_error_is_not_retried(self, hotel):
        tenant = _hotel_mcp(hotel)
        session = _FakeSession(_error("slot already booked"))
        provider = McpBookingProvider(session=session, settings=_settings())

        with pytest.raises(SlotUnavailableError):
            await provider.create_booking(tenant, self._request(tenant))

        assert len(session.calls) == 1

    async def test_deterministic_placeholder_email_when_none_supplied(self, hotel):
        tenant = _hotel_mcp(hotel)
        session = _FakeSession(self._booked(), self._booked())
        provider = McpBookingProvider(session=session, settings=_settings())

        await provider.create_booking(tenant, self._request(tenant, customer_phone="+15551234567"))
        await provider.create_booking(
            tenant,
            self._request(
                tenant,
                customer_phone="+15551234567",
                start=datetime(2050, 9, 6, 9, tzinfo=tenant.tz),
            ),
        )

        email_1 = session.calls[0][1]["attendee"]["email"]
        email_2 = session.calls[1][1]["attendee"]["email"]
        assert email_1 == email_2
        assert email_1.startswith("caller-15551234567@")

    async def test_supplied_email_takes_precedence(self, hotel):
        tenant = _hotel_mcp(hotel)
        session = _FakeSession(self._booked())
        provider = McpBookingProvider(session=session, settings=_settings())

        await provider.create_booking(
            tenant, self._request(tenant, customer_email="jane@example.com")
        )

        assert session.calls[0][1]["attendee"]["email"] == "jane@example.com"

    async def test_job_times_taken_from_response_not_arithmetic(self, hotel):
        tenant = _hotel_mcp(hotel)
        # Response disagrees with our computed end time (e.g. lengthInMinutes
        # was ignored) — response wins, matching CalcomBookingProvider.
        session = _FakeSession(
            self._booked(start="2050-09-05T13:00:00.000Z", end="2050-09-05T13:30:00.000Z")
        )
        provider = McpBookingProvider(session=session, settings=_settings())

        job = await provider.create_booking(tenant, self._request(tenant))

        assert (job.scheduled_end - job.scheduled_start) == timedelta(minutes=30)

    async def test_naive_datetime_is_refused(self, hotel):
        tenant = _hotel_mcp(hotel)
        session = _FakeSession(self._booked())
        provider = McpBookingProvider(session=session, settings=_settings())

        naive_request = self._request(tenant).model_copy(
            update={"start": datetime(2050, 9, 5, 9, 0)}
        )
        with pytest.raises(BookingError):
            await provider.create_booking(tenant, naive_request)
        assert session.calls == []

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("slot already booked", SlotUnavailableError),
            ("no longer available", SlotUnavailableError),
            ("conflict with existing booking", SlotUnavailableError),
            ("unauthorized: bad token", BookingError),
            ("event type not found", BookingError),
            ("internal error", BookingError),
        ],
    )
    async def test_error_mapping(self, hotel, message, expected):
        tenant = _hotel_mcp(hotel)
        session = _FakeSession(_error(message))
        provider = McpBookingProvider(session=session, settings=_settings())

        with pytest.raises(expected):
            await provider.create_booking(tenant, self._request(tenant))

    async def test_raw_provider_text_never_leaks_into_the_exception_message(self, hotel):
        tenant = _hotel_mcp(hotel)
        secret_detail = "internal-stack-trace-xyz"
        session = _FakeSession(_error(secret_detail))
        provider = McpBookingProvider(session=session, settings=_settings())

        with pytest.raises(BookingError) as exc_info:
            await provider.create_booking(tenant, self._request(tenant))
        assert secret_detail not in str(exc_info.value)

    async def test_dead_session_becomes_booking_error_not_a_bare_exception(self, hotel):
        tenant = _hotel_mcp(hotel)
        provider = McpBookingProvider(session=_DeadSession(), settings=_settings())

        with pytest.raises(BookingError):
            await provider.create_booking(tenant, self._request(tenant))

    async def test_cancelled_error_becomes_booking_error_not_a_crashed_turn(self, hotel):
        """Found live (2026-08-03): a slow Cal.com MCP response surfaced as a
        raw asyncio.CancelledError that used to propagate all the way up
        through LangGraph as NodeCancelledError, crashing the whole turn
        instead of degrading to "the calendar isn't responding" like every
        other transport failure."""
        tenant = _hotel_mcp(hotel)
        provider = McpBookingProvider(session=_CancelledSession(), settings=_settings())

        with pytest.raises(BookingError):
            await provider.create_booking(tenant, self._request(tenant))


class TestSessionCaching:
    async def test_session_is_reused_across_calls_within_ttl(self, hotel, monkeypatch):
        tenant = _hotel_mcp(hotel)
        service = tenant.service_by_slug("spa-treatment")
        fake_session = _FakeSession(_ok({}), _ok({}))
        connect_count = {"n": 0}

        @asynccontextmanager
        async def fake_connector(_connection: dict[str, Any]):
            connect_count["n"] += 1
            yield fake_session

        async def fake_connection_for(_tenant_id: str, _settings: Settings) -> dict[str, Any]:
            return {"url": "https://mcp.cal.com/mcp", "headers": {}}

        monkeypatch.setattr("app.tools.booking.mcp_calcom._connection_for", fake_connection_for)

        provider = McpBookingProvider(connector=fake_connector, settings=_settings())
        await provider.check_availability(tenant, service)
        await provider.check_availability(tenant, service)

        assert connect_count["n"] == 1  # reconnected once, reused thereafter
        assert len(fake_session.calls) == 2

    async def test_connector_failure_becomes_booking_error(self, hotel, monkeypatch):
        tenant = _hotel_mcp(hotel)
        service = tenant.service_by_slug("spa-treatment")

        @asynccontextmanager
        async def dead_connector(_connection: dict[str, Any]):
            raise ConnectionError("dead server")
            yield  # pragma: no cover - unreachable; keeps this an async generator

        async def fake_connection_for(_tenant_id: str, _settings: Settings) -> dict[str, Any]:
            return {"url": "https://mcp.cal.com/mcp", "headers": {}}

        monkeypatch.setattr("app.tools.booking.mcp_calcom._connection_for", fake_connection_for)

        provider = McpBookingProvider(connector=dead_connector, settings=_settings())
        with pytest.raises(BookingError):
            await provider.check_availability(tenant, service)

    async def test_unresolved_connection_becomes_booking_error(self, hotel, monkeypatch):
        """`_connection_for` raising (e.g. no OAuth grant, per
        `app/mcp/oauth.py`) must also become a `BookingError`, not escape
        raw — it's called before the connector even runs."""
        tenant = _hotel_mcp(hotel)
        service = tenant.service_by_slug("spa-treatment")

        async def failing_connection_for(_tenant_id: str, _settings: Settings) -> dict[str, Any]:
            raise BookingError("could not resolve this business's calendar authorization")

        monkeypatch.setattr("app.tools.booking.mcp_calcom._connection_for", failing_connection_for)

        provider = McpBookingProvider(settings=_settings())
        with pytest.raises(BookingError):
            await provider.check_availability(tenant, service)

    async def test_aclose_closes_every_cached_session_and_clears_the_cache(
        self, hotel, monkeypatch
    ):
        tenant = _hotel_mcp(hotel)
        service = tenant.service_by_slug("spa-treatment")
        closed = {"n": 0}

        @asynccontextmanager
        async def fake_connector(_connection: dict[str, Any]):
            try:
                yield _FakeSession(_ok({}))
            finally:
                closed["n"] += 1

        async def fake_connection_for(_tenant_id: str, _settings: Settings) -> dict[str, Any]:
            return {"url": "https://mcp.cal.com/mcp", "headers": {}}

        monkeypatch.setattr("app.tools.booking.mcp_calcom._connection_for", fake_connection_for)

        provider = McpBookingProvider(connector=fake_connector, settings=_settings())
        await provider.check_availability(tenant, service)

        await aclose_calcom_mcp_sessions()

        assert closed["n"] == 1
        # A fresh call after shutdown reconnects rather than reusing a
        # closed session — the cache was actually cleared, not just closed.
        await provider.check_availability(tenant, service)
        assert closed["n"] == 1  # second session still open (not closed again)


class TestHandshakeAuthRecovery:
    """Phase 9.4. The MCP handshake carries the Authorization header, so a
    stale access token 401s at *connect* time, before any tool call.

    `_call_tool` has always invalidated on a mid-call 401; this path did not,
    so one bad token poisoned every request for the rest of its ~1h cache
    life — and re-authorizing the tenant did not help, because nothing
    dropped the cached token. Found in production: `check_availability`
    returned nothing at all, for hours, after a grant was replaced.
    """

    @staticmethod
    def _wrapped_401() -> BaseException:
        """A 401 shaped the way the mcp SDK actually delivers one: buried in
        an anyio ExceptionGroup whose own str() mentions neither the status
        nor the URL. Matching the outer message alone never fires."""
        inner = RuntimeError("Client error '401 Unauthorized' for url 'https://mcp.cal.com/mcp'")
        return BaseExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [inner])

    async def test_a_401_handshake_drops_the_token_and_retries_once(self, hotel, monkeypatch):
        tenant = _hotel_mcp(hotel)
        service = tenant.service_by_slug("spa-treatment")
        attempts = {"n": 0}
        invalidated: list[str] = []

        @asynccontextmanager
        async def flaky_connector(_connection: dict[str, Any]):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise self._wrapped_401()
            yield _FakeSession(_ok({}))

        async def fake_connection_for(_tenant_id: str, _settings: Settings) -> dict[str, Any]:
            return {"url": "https://mcp.cal.com/mcp", "headers": {"Authorization": "Bearer x"}}

        monkeypatch.setattr("app.tools.booking.mcp_calcom._connection_for", fake_connection_for)
        monkeypatch.setattr(
            "app.tools.booking.mcp_calcom.invalidate_oauth_token", invalidated.append
        )

        provider = McpBookingProvider(connector=flaky_connector, settings=_settings())
        await provider.check_availability(tenant, service)

        assert attempts["n"] == 2, "should have reconnected with a fresh token"
        assert invalidated == [tenant.tenant_id], "the stale access token must be dropped"

    async def test_a_second_401_gives_up_rather_than_looping(self, hotel, monkeypatch):
        tenant = _hotel_mcp(hotel)
        service = tenant.service_by_slug("spa-treatment")
        attempts = {"n": 0}

        @asynccontextmanager
        async def always_401(_connection: dict[str, Any]):
            attempts["n"] += 1
            raise self._wrapped_401()
            yield  # pragma: no cover - keeps this an async generator

        async def fake_connection_for(_tenant_id: str, _settings: Settings) -> dict[str, Any]:
            return {"url": "https://mcp.cal.com/mcp", "headers": {}}

        monkeypatch.setattr("app.tools.booking.mcp_calcom._connection_for", fake_connection_for)
        monkeypatch.setattr("app.tools.booking.mcp_calcom.invalidate_oauth_token", lambda _t: None)

        provider = McpBookingProvider(connector=always_401, settings=_settings())
        with pytest.raises(BookingError):
            await provider.check_availability(tenant, service)

        assert attempts["n"] == 2, "exactly one retry — a genuinely dead grant must not loop"

    async def test_a_non_auth_failure_is_not_retried(self, hotel, monkeypatch):
        """Retrying a dead server just doubles the caller's wait."""
        tenant = _hotel_mcp(hotel)
        service = tenant.service_by_slug("spa-treatment")
        attempts = {"n": 0}

        @asynccontextmanager
        async def dead(_connection: dict[str, Any]):
            attempts["n"] += 1
            raise ConnectionError("dead server")
            yield  # pragma: no cover

        async def fake_connection_for(_tenant_id: str, _settings: Settings) -> dict[str, Any]:
            return {"url": "https://mcp.cal.com/mcp", "headers": {}}

        monkeypatch.setattr("app.tools.booking.mcp_calcom._connection_for", fake_connection_for)

        provider = McpBookingProvider(connector=dead, settings=_settings())
        with pytest.raises(BookingError):
            await provider.check_availability(tenant, service)

        assert attempts["n"] == 1

    def test_the_401_detector_walks_nested_groups_and_causes(self):
        from app.tools.booking.mcp_calcom import _connect_failure_is_auth

        assert _connect_failure_is_auth(self._wrapped_401())
        assert _connect_failure_is_auth(RuntimeError("invalid_token"))

        chained = RuntimeError("could not connect")
        chained.__cause__ = RuntimeError("401 Unauthorized")
        assert _connect_failure_is_auth(chained)

        assert not _connect_failure_is_auth(ConnectionError("dead server"))
        assert not _connect_failure_is_auth(TimeoutError("too slow"))
