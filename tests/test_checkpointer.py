"""Durable checkpointer fallback behaviour (Phase 4 Step 7) — no live Postgres.

The one thing this module guarantees offline: missing/broken Postgres
config degrades to InMemorySaver with a warning, never a crashed boot.
Actually connecting to a real pooler is live-verification only (see plan).
"""

from __future__ import annotations

import logging

from app.brain.graph import active_checkpointer_name, get_graph, init_postgres_checkpointer
from app.config import reset_settings_cache


def test_default_checkpointer_is_in_memory():
    assert active_checkpointer_name() == "memory"


async def test_init_is_a_noop_without_database_url():
    graph_before = get_graph()
    await init_postgres_checkpointer()
    assert get_graph() is graph_before
    assert active_checkpointer_name() == "memory"


async def test_a_build_failure_falls_back_to_in_memory_with_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", "postgresql://bad:bad@example.invalid:5432/postgres")
    reset_settings_cache()
    try:

        async def _boom(_url: str):
            raise RuntimeError("simulated connection failure")

        monkeypatch.setattr("app.db.checkpointer.build_postgres_saver", _boom)

        graph_before = get_graph()
        with caplog.at_level(logging.WARNING):
            await init_postgres_checkpointer()

        # Falls back — the app must never fail to boot over a durability
        # feature (plan Risk 10).
        assert get_graph() is graph_before
        assert active_checkpointer_name() == "memory"
        assert any("Postgres checkpointer" in record.message for record in caplog.records)
    finally:
        reset_settings_cache()


async def test_a_missing_optional_dependency_falls_back_gracefully(monkeypatch, caplog):
    """`build_postgres_saver` raises ImportError when the `postgres` extra
    isn't installed — must be treated the same as any other build failure,
    not let the ImportError escape and crash the boot."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://bad:bad@example.invalid:5432/postgres")
    reset_settings_cache()
    try:

        async def _missing_dependency(_url: str):
            raise ImportError("No module named 'psycopg'")

        monkeypatch.setattr("app.db.checkpointer.build_postgres_saver", _missing_dependency)

        with caplog.at_level(logging.WARNING):
            await init_postgres_checkpointer()

        assert active_checkpointer_name() == "memory"
    finally:
        reset_settings_cache()
