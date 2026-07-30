"""AnalyticsStore — the admin dashboard's read surface (Phase 8).

`InMemoryStore` aggregates in Python (it's the in-process dev/test store);
`SupabaseStore` reads through the tenant-scoped JWT and the
`security_invoker` views/RPC in `app/db/migrations/0008_analytics.sql`, never
the secret key — the mechanical guard on the tenant-login contract.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from app.config import reset_settings_cache
from app.db.memory_store import get_store
from app.db.models import Call, ChatMessage, ChatSession, Escalation, Job, JobStatus
from app.db.supabase_store import SupabaseStore
from tests.conftest import mock_http

DAY1 = date(2026, 7, 20)
DAY2 = date(2026, 7, 21)


def _dt(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _seed(tenant_id: str = "hotel-mzv") -> None:
    store = get_store()
    store.record_call(
        Call(
            tenant_id=tenant_id,
            provider_call_id="c1",
            duration_seconds=60,
            cost_usd=0.5,
            created_at=_dt(DAY1, 9),
        )
    )
    store.record_call(
        Call(
            tenant_id=tenant_id,
            provider_call_id="c2",
            duration_seconds=120,
            cost_usd=0.7,
            created_at=_dt(DAY1, 10),
        )
    )
    store.record_call(
        Call(
            tenant_id=tenant_id,
            provider_call_id="c3",
            duration_seconds=30,
            cost_usd=0.1,
            created_at=_dt(DAY2, 9),
        )
    )
    store.add(
        Job(
            tenant_id=tenant_id,
            customer_name="A",
            customer_phone="+1",
            address="",
            service_slug="room-reservation",
            service_name="Room reservation",
            scheduled_start=_dt(DAY1, 14),
            scheduled_end=_dt(DAY1, 15),
            created_at=_dt(DAY1, 9),
        )
    )
    store.add(
        Job(
            tenant_id=tenant_id,
            customer_name="B",
            customer_phone="+1",
            address="",
            service_slug="room-reservation",
            service_name="Room reservation",
            status=JobStatus.CANCELLED,
            scheduled_start=_dt(DAY1, 16),
            scheduled_end=_dt(DAY1, 17),
            created_at=_dt(DAY1, 11),
        )
    )
    store.add(
        Job(
            tenant_id=tenant_id,
            customer_name="C",
            customer_phone="+1",
            address="",
            service_slug="spa-treatment",
            service_name="Spa treatment",
            status=JobStatus.COMPLETED,
            scheduled_start=_dt(DAY2, 14),
            scheduled_end=_dt(DAY2, 15),
            created_at=_dt(DAY2, 9),
        )
    )
    store.record_escalation(
        Escalation(
            tenant_id=tenant_id,
            reason="gas leak",
            transferred_to="+1555",
            created_at=_dt(DAY1, 9),
        )
    )
    store.record_escalation(
        Escalation(
            tenant_id=tenant_id,
            reason="medical",
            transferred_to="+1555",
            created_at=_dt(DAY1, 10),
        )
    )
    store.record_escalation(
        Escalation(
            tenant_id=tenant_id,
            reason="gas leak",
            transferred_to="+1555",
            created_at=_dt(DAY2, 9),
        )
    )
    store.start_chat_session(
        ChatSession(id="web_day1", tenant_id=tenant_id, started_at=_dt(DAY1, 9))
    )
    store.start_chat_session(
        ChatSession(id="web_day2", tenant_id=tenant_id, started_at=_dt(DAY2, 9))
    )
    store.record_chat_message(
        ChatMessage(
            tenant_id=tenant_id, session_id="web_day1", role="user", created_at=_dt(DAY1, 9)
        )
    )
    store.record_chat_message(
        ChatMessage(
            tenant_id=tenant_id,
            session_id="web_day1",
            role="assistant",
            created_at=_dt(DAY1, 9),
        )
    )
    store.record_chat_message(
        ChatMessage(
            tenant_id=tenant_id, session_id="web_day2", role="user", created_at=_dt(DAY2, 9)
        )
    )


class TestInMemoryStoreAggregation:
    def test_tenant_metrics_totals_across_the_window(self):
        _seed()
        metrics = get_store().tenant_metrics("hotel-mzv", since=DAY1, until=DAY2)

        assert metrics.tenant_id == "hotel-mzv"
        assert metrics.calls == 3
        assert metrics.call_seconds == 210  # 60 + 120 + 30
        assert metrics.cost_usd == pytest.approx(1.3)  # 0.5 + 0.7 + 0.1
        assert metrics.escalations == 3
        assert metrics.chat_sessions == 2
        assert metrics.chat_messages == 3

    def test_a_cancelled_job_is_excluded_from_the_bookings_count(self):
        """3 jobs seeded, one of them cancelled — the headline `jobs` number
        means "bookings that still stand", not "booking attempts"."""
        _seed()
        metrics = get_store().tenant_metrics("hotel-mzv", since=DAY1, until=DAY2)
        assert metrics.jobs == 2

    def test_daily_series_buckets_correctly_by_day(self):
        _seed()
        series = get_store().daily_series("hotel-mzv", since=DAY1, until=DAY2)

        assert [d.day for d in series] == [DAY1, DAY2]
        day1, day2 = series

        assert day1.calls == 2
        assert day1.call_seconds == 180
        assert day1.cost_usd == pytest.approx(1.2)
        assert day1.jobs == 1  # the cancelled one on day1 is excluded
        assert day1.escalations == 2  # gas leak + medical, both counted regardless of reason
        assert day1.chat_sessions == 1
        assert day1.chat_messages == 2

        assert day2.calls == 1
        assert day2.call_seconds == 30
        assert day2.jobs == 1
        assert day2.escalations == 1
        assert day2.chat_sessions == 1
        assert day2.chat_messages == 1

    def test_escalations_count_regardless_of_reason(self):
        """Two different reasons on day 1 (gas leak, medical) both land in
        that day's total — the headline number collapses across reason, the
        same way it collapses across a job's status; the reason breakdown
        itself lives in the `daily_escalation_stats` SQL view, queried
        directly by the admin API's breakdown panel, not through this
        protocol."""
        _seed()
        metrics = get_store().tenant_metrics("hotel-mzv", since=DAY1, until=DAY1)
        assert metrics.escalations == 2

    def test_window_excludes_days_outside_it(self):
        _seed()
        metrics = get_store().tenant_metrics("hotel-mzv", since=DAY2, until=DAY2)
        assert metrics.calls == 1
        assert metrics.escalations == 1

    def test_list_recent_calls_has_no_transcript_field(self):
        """Explicit assert, not an inference — this must fail loudly the day
        someone adds `transcript` back to the list-shaped response."""
        _seed()
        store = get_store()
        store.record_call(
            Call(
                tenant_id="hotel-mzv",
                provider_call_id="c-with-transcript",
                transcript="hello there, this is a private conversation",
            )
        )
        summaries = store.list_recent_calls("hotel-mzv", limit=10)
        assert summaries
        assert not hasattr(summaries[0], "transcript")
        assert not hasattr(summaries[0], "recording_url")

    def test_list_recent_calls_orders_newest_first_and_respects_limit(self):
        _seed()
        summaries = get_store().list_recent_calls("hotel-mzv", limit=2)
        assert len(summaries) == 2
        assert summaries[0].created_at >= summaries[1].created_at

    def test_get_call_returns_the_full_record_with_transcript(self):
        store = get_store()
        call = store.record_call(
            Call(tenant_id="hotel-mzv", provider_call_id="c-full", transcript="the full text")
        )
        fetched = store.get_call("hotel-mzv", call.id)
        assert fetched is not None
        assert fetched.transcript == "the full text"

    def test_get_call_returns_none_for_unknown_id(self):
        assert get_store().get_call("hotel-mzv", "call_doesnotexist") is None

    def test_list_chat_sessions_orders_newest_first(self):
        _seed()
        sessions = get_store().list_chat_sessions("hotel-mzv", limit=10)
        assert [s.id for s in sessions] == ["web_day2", "web_day1"]

    def test_tenant_isolation_across_analytics_methods(self):
        _seed("hotel-mzv")
        _seed("northside-plumbing")
        hotel_metrics = get_store().tenant_metrics("hotel-mzv", since=DAY1, until=DAY2)
        northside_metrics = get_store().tenant_metrics("northside-plumbing", since=DAY1, until=DAY2)
        assert hotel_metrics.calls == northside_metrics.calls == 3
        assert get_store().get_call("hotel-mzv", "does-not-exist") is None


# --- SupabaseStore: request shape, tenant JWT, never the secret key ---------

_APIKEY_HEADERS = {"apikey": "test-anon-key"}


def _mock_supabase(handler):
    return mock_http(handler, headers=_APIKEY_HEADERS)


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-jwt-secret")
    reset_settings_cache()
    yield
    reset_settings_cache()


class TestSupabaseStoreAnalytics:
    async def test_atenant_metrics_posts_to_the_rpc_with_tenant_jwt(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(
                200,
                json=[
                    {
                        "tenant_id": "hotel-mzv",
                        "calls": 3,
                        "call_seconds": 210,
                        "cost_usd": 1.3,
                        "jobs": 2,
                        "escalations": 3,
                        "chat_sessions": 2,
                        "chat_messages": 3,
                    }
                ],
            )

        client, _ = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        metrics = await store.atenant_metrics("hotel-mzv", since=DAY1, until=DAY2)

        request = captured["request"]
        assert request.url.path == "/rpc/tenant_metrics"
        # A tenant JWT, never the secret/service_role key — the mechanical
        # guard on the tenant-login contract.
        auth_header = request.headers["authorization"]
        assert auth_header.startswith("Bearer ")
        assert metrics.jobs == 2
        assert metrics.chat_messages == 3

    async def test_atenant_metrics_defaults_to_zero_on_an_empty_result(self):
        client, _ = _mock_supabase(lambda request: httpx.Response(200, json=[]))
        store = SupabaseStore(client=client)
        metrics = await store.atenant_metrics("hotel-mzv", since=DAY1, until=DAY2)
        assert metrics == metrics.__class__(tenant_id="hotel-mzv")

    async def test_adaily_series_merges_four_views_by_day(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/daily_call_stats":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "tenant_id": "hotel-mzv",
                            "day": "2026-07-20",
                            "calls": 2,
                            "total_seconds": 180,
                            "cost_usd": 1.2,
                        }
                    ],
                )
            if path == "/daily_job_stats":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "tenant_id": "hotel-mzv",
                            "day": "2026-07-20",
                            "status": "scheduled",
                            "channel": "chat",
                            "jobs": 1,
                        },
                        {
                            "tenant_id": "hotel-mzv",
                            "day": "2026-07-20",
                            "status": "cancelled",
                            "channel": "chat",
                            "jobs": 1,
                        },
                    ],
                )
            if path == "/daily_chat_stats":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "tenant_id": "hotel-mzv",
                            "day": "2026-07-20",
                            "sessions": 1,
                            "messages": 2,
                        }
                    ],
                )
            if path == "/daily_escalation_stats":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "tenant_id": "hotel-mzv",
                            "day": "2026-07-20",
                            "reason": "gas leak",
                            "channel": "voice",
                            "escalations": 1,
                        },
                        {
                            "tenant_id": "hotel-mzv",
                            "day": "2026-07-20",
                            "reason": "medical",
                            "channel": "voice",
                            "escalations": 1,
                        },
                    ],
                )
            raise AssertionError(f"unexpected path {path}")

        client, requests = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        series = await store.adaily_series("hotel-mzv", since=DAY1, until=DAY2)

        assert len(series) == 1
        day = series[0]
        assert day.day == DAY1
        assert day.calls == 2
        assert day.call_seconds == 180
        assert day.jobs == 1  # the cancelled row is excluded
        assert day.escalations == 2  # both reasons counted
        assert day.chat_sessions == 1
        assert day.chat_messages == 2
        # Every one of the four requests carried the explicit tenant_id
        # filter, even though RLS also enforces it — convention #3.
        assert len(requests) == 4
        for request in requests:
            assert "tenant_id=eq.hotel-mzv" in str(request.url)

    async def test_alist_recent_calls_excludes_transcript_at_the_query_level(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "call_1",
                        "tenant_id": "hotel-mzv",
                        "provider_call_id": "p1",
                        "channel": "voice",
                        "created_at": "2026-07-20T09:00:00Z",
                    }
                ],
            )

        client, _ = _mock_supabase(handler)
        store = SupabaseStore(client=client)

        summaries = await store.alist_recent_calls("hotel-mzv", limit=10)

        select_param = captured["request"].url.params["select"]
        assert "transcript" not in select_param
        assert "recording_url" not in select_param
        assert len(summaries) == 1
        assert not hasattr(summaries[0], "transcript")

    async def test_aget_call_returns_the_full_row(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "call_1",
                        "tenant_id": "hotel-mzv",
                        "provider_call_id": "p1",
                        "channel": "voice",
                        "transcript": "full text",
                        "created_at": "2026-07-20T09:00:00Z",
                    }
                ],
            )

        client, _ = _mock_supabase(handler)
        store = SupabaseStore(client=client)
        call = await store.aget_call("hotel-mzv", "call_1")
        assert call is not None
        assert call.transcript == "full text"

    async def test_aget_call_returns_none_when_absent(self):
        client, _ = _mock_supabase(lambda request: httpx.Response(200, json=[]))
        store = SupabaseStore(client=client)
        assert await store.aget_call("hotel-mzv", "call_doesnotexist") is None

    async def test_alist_chat_sessions_orders_newest_first_via_query(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(
                200,
                json=[
                    {"id": "web_2", "tenant_id": "hotel-mzv", "started_at": "2026-07-21T09:00:00Z"},
                    {"id": "web_1", "tenant_id": "hotel-mzv", "started_at": "2026-07-20T09:00:00Z"},
                ],
            )

        client, _ = _mock_supabase(handler)
        store = SupabaseStore(client=client)
        sessions = await store.alist_chat_sessions("hotel-mzv", limit=10)

        assert [s.id for s in sessions] == ["web_2", "web_1"]
        assert captured["request"].url.params["order"] == "started_at.desc"
