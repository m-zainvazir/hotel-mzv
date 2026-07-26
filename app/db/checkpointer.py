"""Durable conversation memory via LangGraph's Postgres checkpointer
(Phase 4 Step 7).

Optional extra: `pip install -e ".[postgres]"`. Without `DATABASE_URL`, or if
the extra isn't installed, or if the live connection fails at startup, the
graph stays on `InMemorySaver` — see `app/brain/graph.py::init_postgres_checkpointer`,
which decides how to degrade. This module's only job is to build the saver
or raise.

Checkpoint tables live in a dedicated `langgraph` schema, never `public`:
PostgREST exposes every table in `public`, and `AsyncPostgresSaver.setup()`
creates unqualified `checkpoints` / `checkpoint_blobs` / `checkpoint_writes`
tables — left in `public`, every conversation transcript would be readable
over the REST API with the anon key, silently, since nothing would error.

Isolation here is by thread-id prefix (`f"{tenant}:vapi:{call.id}"`, ...),
**not** RLS. This connection authenticates as the database's own Postgres
role over the wire protocol, not through PostgREST as a `app_backend`-scoped
request — the JWT/RLS machinery in `app/db/auth.py` and
`app/db/migrations/0002_rls.sql` does not cover these tables at all.

Connection details that are live-only failures if wrong (plan Risk 8):
  * Supavisor **session-mode pooler** host (`aws-0-<region>.pooler.supabase.com`,
    port 5432) — never the direct `db.<ref>.supabase.co` host, which has
    resolved to IPv6-only since Jan 2024 and most hosting platforms have no
    IPv6 egress.
  * **Not** the 6543 transaction-mode port — it doesn't support prepared
    statements, and `AsyncPostgresSaver` dies on it with
    `DuplicatePreparedStatementError` / `InvalidSqlStatementName`.
  * `prepare_threshold=0` regardless, as belt-and-braces.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_pool = None


async def build_postgres_saver(database_url: str):
    """Open a small connection pool and run `AsyncPostgresSaver.setup()`.

    Raises on any failure — a missing optional dependency, a bad connection,
    a `.setup()` error. The caller decides how to degrade; this function's
    only job is to build a working saver or fail loudly.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    global _pool
    pool = AsyncConnectionPool(
        conninfo=database_url,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": "-c search_path=langgraph",
        },
        # Free tier is 500MB shared RAM; a default pool of 4+ per instance
        # burns connections for no benefit at our concurrency.
        min_size=1,
        max_size=3,
        open=False,
    )
    await pool.open()

    async with pool.connection() as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS langgraph")

    saver = AsyncPostgresSaver(conn=pool)
    await saver.setup()

    _pool = pool
    logger.info("durable Postgres checkpointer connected (schema=langgraph)")
    return saver


async def close_postgres_pool() -> None:
    """Call on process shutdown so the connection pool closes cleanly."""
    global _pool
    if _pool is not None:
        await _pool.close()
    _pool = None


def reset_postgres_pool_ref() -> None:
    """Test hook — drop the module-level reference without awaiting a close."""
    global _pool
    _pool = None
