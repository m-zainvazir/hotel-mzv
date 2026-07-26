"""SupabaseStore: PostgREST request shape, response mapping, error handling.

No network — every client is built with httpx.MockTransport via mock_http().
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.config import reset_settings_cache
from app.db.models import (
    Call,
    ChatMessage,
    ChatSession,
    Escalation,
    Job,
    JobStatus,
    OutboundMessage,
)
from app.db.supabase_store import SupabaseStore, SupabaseStoreError
from tests.conftest import mock_http

_APIKEY_HEADERS = {"apikey": "test-anon-key"}


def _mock_supabase(handler):
    """mock_http, pre-loaded with the `apikey` header a real settings-built
    client would carry (an injected client otherwise has none — see
    SupabaseStore._get_client)."""
    return mock_http(handler, headers=_APIKEY_HEADERS)


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-jwt-secret")
    reset_settings_cache()
    yield
    reset_settings_cache()


def _job(**overrides) -> Job:
    base = dict(
        id="job_abc1234567",
        tenant_id="hotel-mzv",
        customer_name="Jane Doe",
        customer_phone="+15551234567",
        address="123 Main St",
        service_slug="room-reservation",
        service_name="Room Reservation",
        scheduled_start=datetime(2026, 8, 1, 14, 0, tzinfo=UTC),
        scheduled_end=datetime(2026, 8, 1, 14, 30, tzinfo=UTC),
        created_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
    )
    base.update(overrides)
    return Job(**base)


def _row_response(status: int, row: dict) -> httpx.Response:
    return httpx.Response(status, json=[row])


class TestAdd:
    async def test_request_shape_and_response_mapping(self):
        job = _job()

        def handler(req: httpx.Request) -> httpx.Response:
            sent = json.loads(req.content)
            return _row_response(201, sent)

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        result = await store.aadd(job)

        assert len(requests) == 1
        request = requests[0]
        assert request.url.path == "/jobs"
        assert request.method == "POST"
        assert request.headers["prefer"] == "return=representation"
        assert request.headers["authorization"].startswith("Bearer ")
        assert result.id == job.id
        assert result.tenant_id == job.tenant_id

    async def test_empty_response_raises(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json=[])

        client, _ = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        with pytest.raises(SupabaseStoreError):
            await store.aadd(_job())


class TestGet:
    async def test_request_shape(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        result = await store.aget("hotel-mzv", "job_abc1234567")

        assert result is None
        request = requests[0]
        assert request.method == "GET"
        params = dict(request.url.params)
        assert params["tenant_id"] == "eq.hotel-mzv"
        assert params["id"] == "eq.job_abc1234567"

    async def test_found_row_is_mapped_back_to_a_job(self):
        row = _job().model_dump(mode="json")
        row["status"] = "scheduled"

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[row])

        client, _ = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        result = await store.aget("hotel-mzv", "job_abc1234567")
        assert result is not None
        assert result.id == "job_abc1234567"
        assert result.status is JobStatus.SCHEDULED


class TestUpdate:
    async def test_zero_row_patch_raises_key_error(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        with pytest.raises(KeyError):
            await store.aupdate(_job())

        request = requests[0]
        assert request.method == "PATCH"
        params = dict(request.url.params)
        assert params["tenant_id"] == "eq.hotel-mzv"
        assert params["id"] == "eq.job_abc1234567"

    async def test_matched_row_is_returned(self):
        row = _job(status=JobStatus.CANCELLED).model_dump(mode="json")
        row["status"] = "cancelled"

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[row])

        client, _ = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        result = await store.aupdate(_job(status=JobStatus.CANCELLED))
        assert result.status is JobStatus.CANCELLED


class TestListJobs:
    async def test_since_and_until_become_repeated_params(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        since = datetime(2026, 7, 1, tzinfo=UTC)
        until = datetime(2026, 8, 1, tzinfo=UTC)
        await store.alist_jobs("hotel-mzv", since=since, until=until)

        request = requests[0]
        params = list(request.url.params.multi_items())
        keys = [k for k, _ in params]
        assert keys.count("tenant_id") == 1
        assert ("scheduled_end", f"gt.{since.isoformat()}") in params
        assert ("scheduled_start", f"lt.{until.isoformat()}") in params


class TestScheduledBetween:
    async def test_request_uses_overlap_predicate_and_status_filter(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        start = datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
        end = datetime(2026, 8, 1, 15, 0, tzinfo=UTC)
        await store.ascheduled_between("hotel-mzv", start, end)

        params = dict(requests[0].url.params)
        assert params["tenant_id"] == "eq.hotel-mzv"
        assert params["status"] == "eq.scheduled"
        assert params["scheduled_start"] == f"lt.{end.isoformat()}"
        assert params["scheduled_end"] == f"gt.{start.isoformat()}"


class TestMessages:
    async def test_record_message_request_shape(self):
        message = OutboundMessage(
            tenant_id="hotel-mzv", to="+15550001111", body="hi", kind="confirmation"
        )

        def handler(_req: httpx.Request) -> httpx.Response:
            return _row_response(201, message.model_dump(mode="json"))

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        result = await store.arecord_message(message)

        assert requests[0].url.path == "/messages"
        assert requests[0].headers["prefer"] == "return=representation"
        assert result.id == message.id

    async def test_list_messages_filters_by_tenant(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        await store.alist_messages("hotel-mzv")
        assert dict(requests[0].url.params)["tenant_id"] == "eq.hotel-mzv"


class TestRecordCall:
    async def test_no_existing_call_inserts(self):
        call = Call(tenant_id="hotel-mzv", provider_call_id="vapi_call_1")
        calls_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls_made.append(req)
            if req.method == "GET":
                return httpx.Response(200, json=[])
            return _row_response(201, call.model_dump(mode="json"))

        client, _ = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        result = await store.arecord_call(call)

        methods = [r.method for r in calls_made]
        assert methods == ["GET", "POST"]
        assert result.id == call.id

    async def test_existing_call_patches_and_preserves_the_original_id(self):
        existing_id = "call_original00"
        new_call = Call(
            tenant_id="hotel-mzv", provider_call_id="vapi_call_1", ended_reason="hangup"
        )
        calls_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls_made.append(req)
            if req.method == "GET":
                existing_row = new_call.model_dump(mode="json")
                existing_row["id"] = existing_id
                return httpx.Response(200, json=[existing_row])
            # PATCH: assert it targets the existing id, not the new object's id.
            assert dict(req.url.params)["id"] == f"eq.{existing_id}"
            patched_row = new_call.model_dump(mode="json")
            patched_row["id"] = existing_id
            return httpx.Response(200, json=[patched_row])

        client, _ = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        result = await store.arecord_call(new_call)

        methods = [r.method for r in calls_made]
        assert methods == ["GET", "PATCH"]
        assert result.id == existing_id


class TestEscalations:
    async def test_record_escalation_request_shape(self):
        escalation = Escalation(tenant_id="hotel-mzv", reason="gas leak", transferred_to="+1555")

        def handler(_req: httpx.Request) -> httpx.Response:
            return _row_response(201, escalation.model_dump(mode="json"))

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        result = await store.arecord_escalation(escalation)
        assert requests[0].url.path == "/escalations"
        assert result.id == escalation.id

    async def test_list_escalations_filters_by_tenant(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        await store.alist_escalations("hotel-mzv")
        assert dict(requests[0].url.params)["tenant_id"] == "eq.hotel-mzv"


class TestChatTranscripts:
    async def test_start_chat_session_request_shape(self):
        session = ChatSession(
            id="web_abc123456789", tenant_id="hotel-mzv", widget_key="pk_widget_hotelmzv_demo"
        )

        def handler(_req: httpx.Request) -> httpx.Response:
            return _row_response(201, session.model_dump(mode="json"))

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        result = await store.astart_chat_session(session)
        assert requests[0].url.path == "/chat_sessions"
        assert requests[0].headers["prefer"] == "return=representation"
        assert result.id == session.id

    async def test_record_chat_message_request_shape(self):
        message = ChatMessage(
            tenant_id="hotel-mzv", session_id="web_abc123456789", role="user", content="hi"
        )

        def handler(req: httpx.Request) -> httpx.Response:
            sent = json.loads(req.content)
            assert sent["role"] == "user"
            assert sent["session_id"] == "web_abc123456789"
            return _row_response(201, message.model_dump(mode="json"))

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        result = await store.arecord_chat_message(message)
        assert requests[0].url.path == "/chat_messages"
        assert result.id == message.id

    async def test_get_chat_session_request_shape(self):
        session = ChatSession(
            id="web_abc123456789", tenant_id="hotel-mzv", widget_key="pk_widget_hotelmzv_demo"
        )

        def handler(_req: httpx.Request) -> httpx.Response:
            return _row_response(200, session.model_dump(mode="json"))

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        result = await store.aget_chat_session("hotel-mzv", "web_abc123456789")
        params = dict(requests[0].url.params)
        assert params["tenant_id"] == "eq.hotel-mzv"
        assert params["id"] == "eq.web_abc123456789"
        assert result.id == session.id

    async def test_get_chat_session_returns_none_when_absent(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client, _ = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        assert await store.aget_chat_session("hotel-mzv", "web_missing") is None

    async def test_list_chat_messages_filters_by_tenant_and_session(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        await store.alist_chat_messages("hotel-mzv", "web_abc123456789")
        params = dict(requests[0].url.params)
        assert params["tenant_id"] == "eq.hotel-mzv"
        assert params["session_id"] == "eq.web_abc123456789"

    async def test_start_chat_session_raises_on_empty_response(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json=[])

        client, _ = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        with pytest.raises(SupabaseStoreError):
            await store.astart_chat_session(
                ChatSession(id="web_x", tenant_id="hotel-mzv", widget_key="pk_x")
            )


class TestErrorMapping:
    async def test_5xx_raises_supabase_store_error(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="internal error")

        client, _ = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        with pytest.raises(SupabaseStoreError):
            await store.aget("hotel-mzv", "job_abc1234567")

    async def test_timeout_raises_supabase_store_error(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("boom")

        client, _ = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        with pytest.raises(SupabaseStoreError):
            await store.aget("hotel-mzv", "job_abc1234567")


class TestTenantIsolationAcrossEveryMethod:
    """No request may ever go out without a tenant_id=eq. filter — the
    offline half of tenant isolation (the live half is RLS itself)."""

    @pytest.mark.parametrize(
        "call",
        [
            lambda store: store.aget("hotel-mzv", "job_1"),
            lambda store: store.alist_jobs("hotel-mzv"),
            lambda store: store.ascheduled_between(
                "hotel-mzv", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 2, tzinfo=UTC)
            ),
            lambda store: store.alist_messages("hotel-mzv"),
            lambda store: store.alist_escalations("hotel-mzv"),
            lambda store: store.alist_chat_messages("hotel-mzv", "web_abc123456789"),
            lambda store: store.aget_chat_session("hotel-mzv", "web_abc123456789"),
        ],
    )
    async def test_read_methods_always_filter_by_tenant_id(self, call):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        await call(store)

        assert requests, "expected at least one request"
        for request in requests:
            params = dict(request.url.params)
            assert "tenant_id" in params, f"{request.url} has no tenant_id filter"
            assert params["tenant_id"].startswith("eq."), f"{request.url} tenant_id isn't eq."

    async def test_jwt_authorization_header_is_always_present(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        await store.aget("hotel-mzv", "job_1")

        assert requests[0].headers["authorization"].startswith("Bearer ")
