"""Guards that every table created under app/db/migrations/ actually gets RLS.

Phase 4 plan Risk 3 ("decorative RLS"): a table that gets ENABLE without
FORCE, or a policy without a GRANT, fails silently — either the table owner
bypasses the policy, or every read comes back empty and looks exactly like
"no data yet" (the same failure mode as plan Risk 6, Phase 3). This test
can't verify RLS actually *works* (that needs a live database — see the
plan's live-verification checklist), but it permanently closes "someone
added a table and forgot RLS", which is the real long-run failure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "app" / "db" / "migrations"

_TABLE_RE = re.compile(r"create table public\.(\w+)", re.IGNORECASE)


def _all_migration_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
    )


def _created_tables() -> list[str]:
    return _TABLE_RE.findall(_all_migration_text())


def test_migrations_directory_is_not_empty():
    assert list(MIGRATIONS_DIR.glob("*.sql")), "expected numbered .sql migrations on disk"


def test_every_table_has_a_name():
    assert _created_tables(), "the table-detection regex found nothing — migrations changed shape"


@pytest.mark.parametrize("table", _created_tables() or ["<no tables found>"])
def test_table_enables_and_forces_rls(table):
    text = _all_migration_text()
    enable_pattern = rf"alter table public\.{table}\s+enable row level security"
    assert re.search(enable_pattern, text, re.IGNORECASE), (
        f"public.{table} has no `enable row level security` statement"
    )
    force_pattern = rf"alter table public\.{table}\s+force row level security"
    assert re.search(force_pattern, text, re.IGNORECASE), (
        f"public.{table} is missing `force row level security` — without FORCE, the table "
        "owner (e.g. an admin SQL-editor session) silently bypasses every policy below"
    )


@pytest.mark.parametrize("table", _created_tables() or ["<no tables found>"])
def test_table_has_a_tenant_scoped_policy(table):
    text = _all_migration_text()
    policy_match = re.search(
        rf"create policy \w+ on public\.{table}\b.*?;", text, re.IGNORECASE | re.DOTALL
    )
    assert policy_match, f"public.{table} has no `create policy ... on public.{table}` statement"
    assert "tenant_id" in policy_match.group(0), (
        f"public.{table}'s policy doesn't reference tenant_id — RLS with no tenant "
        "predicate isn't isolation, it's a no-op"
    )


@pytest.mark.parametrize("table", _created_tables() or ["<no tables found>"])
def test_table_grants_to_app_backend(table):
    text = _all_migration_text()
    assert re.search(rf"grant [\w, ]+ on public\.{table} to app_backend", text, re.IGNORECASE), (
        f"public.{table} has RLS but no GRANT to app_backend — a perfect policy with no "
        "grant is a 403 that reads exactly like an auth bug"
    )
