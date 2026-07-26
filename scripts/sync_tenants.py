"""Push content/tenants/*.json into Supabase (Phase 4).

    python -m scripts.sync_tenants                    # sync every tenant
    python -m scripts.sync_tenants --tenant hotel-mzv  # sync just one

The tenant *read* path stays on the JSON files for Phase 4 — the brain never
queries `tenants`/`services` today (see CLAUDE.md's "On the tenant read
path" note). This exists so onboarding/audit data is durable and those
tables are ready for the eventual read-path flip (`TENANT_SOURCE=supabase`
in .env, once that's verified live). Every write is an upsert, so this is
safe to re-run any time a tenant JSON file changes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from rich.console import Console

from app.config import get_settings
from app.logging_config import configure_logging, force_utf8_console
from app.tenancy.loader import get_repository, get_tenant_config
from app.tenancy.sync import TenantSyncError, sync_tenant

console = Console()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", help="sync only this tenant_id (default: every tenant)")
    return parser.parse_args(argv)


async def _run(tenant_ids: list[str]) -> int:
    failures = 0
    for tenant_id in tenant_ids:
        try:
            tenant = get_tenant_config(tenant_id)
            await sync_tenant(tenant)
            console.print(f"[green]✓[/green] {tenant_id}")
        except TenantSyncError as exc:
            console.print(f"[bold red]✗ {tenant_id}:[/bold red] {exc}")
            failures += 1
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    force_utf8_console()
    configure_logging()
    args = _parse_args(argv)
    settings = get_settings()

    if not settings.supabase_url or not settings.supabase_secret_key:
        console.print("[bold red]error:[/bold red] SUPABASE_URL / SUPABASE_SECRET_KEY must be set")
        return 1

    try:
        tenant_ids = [args.tenant] if args.tenant else get_repository().list_ids()
    except LookupError as exc:
        console.print(f"[bold red]error:[/bold red] {exc}")
        return 1

    if not tenant_ids:
        console.print("[dim]no tenants found[/dim]")
        return 0

    return asyncio.run(_run(tenant_ids))


if __name__ == "__main__":
    sys.exit(main())
