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
_VIEW_RE = re.compile(r"create view public\.(\w+)", re.IGNORECASE)


def _all_migration_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
    )


def _created_tables() -> list[str]:
    return _TABLE_RE.findall(_all_migration_text())


def _created_views() -> list[str]:
    return _VIEW_RE.findall(_all_migration_text())


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


@pytest.mark.parametrize("view", _created_views() or ["<no views found>"])
def test_view_is_security_invoker(view):
    """Phase 8: a plain view in `public` is PostgREST-exposed automatically
    and runs with the OWNER's privileges — it does NOT apply the underlying
    tables' RLS. Without `security_invoker = true` an analytics view hands
    every tenant's data to anything holding the anon key, and this is the
    only thing that would ever catch that: the table lint above only
    inspects `create table`."""
    text = _all_migration_text()
    match = re.search(rf"create view public\.{view}\b.*?;", text, re.IGNORECASE | re.DOTALL)
    assert match, f"public.{view} has no bounded `create view ... ;` statement"
    assert re.search(r"security_invoker\s*=\s*true", match.group(0), re.IGNORECASE), (
        f"public.{view} has no `security_invoker = true` — it would run with the "
        "view owner's privileges, not the querying role's, defeating RLS entirely"
    )


@pytest.mark.parametrize("view", _created_views() or ["<no views found>"])
def test_view_grants_to_app_backend(view):
    text = _all_migration_text()
    assert re.search(rf"grant select on public\.{view} to app_backend", text, re.IGNORECASE), (
        f"public.{view} has no `grant select ... to app_backend`"
    )
    # The tenant-login contract (plans/phase8.md): these views must never be
    # granted to `authenticated` — that would hand a future Supabase-Auth
    # end user every grant written for this backend, exactly what
    # 0002_rls.sql's app_backend-not-authenticated choice exists to prevent.
    assert not re.search(rf"grant [\w, ]+ on public\.{view} to authenticated", text, re.IGNORECASE), (
        f"public.{view} is granted to `authenticated` — that role is for GoTrue end "
        "users and must never inherit this backend's grants"
    )
