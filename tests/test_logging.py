"""Phase 7 Step 5 — structured logs, request correlation, and the two PII
leaks fixed alongside them."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.logging_config import _JsonFormatter
from app.main import app
from app.middleware import RequestIdFilter, _request_id_var


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_produces_parseable_json_with_the_request_id():
    record = _record(request_id="req-123")
    parsed = json.loads(_JsonFormatter().format(record))
    assert parsed["message"] == "hello world"
    assert parsed["request_id"] == "req-123"
    assert parsed["level"] == "INFO"


def test_json_formatter_defaults_request_id_when_absent():
    parsed = json.loads(_JsonFormatter().format(_record()))
    assert parsed["request_id"] == "-"


def test_request_id_filter_reads_the_contextvar():
    token = _request_id_var.set("ctx-abc")
    try:
        record = _record()
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "ctx-abc"
    finally:
        _request_id_var.reset(token)


def test_an_inbound_request_id_is_echoed_back(client):
    response = client.get("/health", headers={"X-Request-Id": "caller-supplied-id"})
    assert response.headers["x-request-id"] == "caller-supplied-id"


def test_a_request_id_is_generated_when_none_is_supplied(client):
    response = client.get("/health")
    assert response.headers["x-request-id"]  # non-empty, some generated uuid


async def test_sms_body_is_logged_at_debug_not_info(caplog):
    from app.db.memory_store import InMemoryStore
    from app.tenancy.loader import get_tenant_config
    from app.tools.messaging.stub import StubNotifier

    notifier = StubNotifier(store=InMemoryStore())
    hotel = get_tenant_config("hotel-mzv")

    with caplog.at_level(logging.INFO):
        await notifier.send_sms(hotel, to="+15550001111", body="Jane Doe, 3pm Tuesday")
    assert not any("Jane Doe" in r.getMessage() for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await notifier.send_sms(hotel, to="+15550001111", body="Jane Doe, 3pm Tuesday")
    assert any("Jane Doe" in r.getMessage() for r in caplog.records)


async def test_checkpointer_failure_log_never_contains_the_database_password(monkeypatch, caplog):
    from app.brain.graph import init_postgres_checkpointer
    from app.config import reset_settings_cache

    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://appuser:sup3rs3cret@example.invalid:5432/postgres"
    )
    reset_settings_cache()

    async def _boom(_url: str):
        # Real psycopg error paths can embed the conninfo -- password
        # included -- in the exception's own message.
        raise RuntimeError(
            "connection failed: postgresql://appuser:sup3rs3cret@example.invalid:5432/postgres"
        )

    monkeypatch.setattr("app.db.checkpointer.build_postgres_saver", _boom)
    try:
        with caplog.at_level(logging.WARNING):
            await init_postgres_checkpointer()
        full_text = "\n".join(r.getMessage() for r in caplog.records)
        assert "sup3rs3cret" not in full_text
        assert "***" in full_text
    finally:
        reset_settings_cache()
