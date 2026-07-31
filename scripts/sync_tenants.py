"""Push content/tenants/*.json into Supabase (Phase 4), or pull it back down.

    python -m scripts.sync_tenants                    # sync every tenant
    python -m scripts.sync_tenants --tenant hotel-mzv  # sync just one
    python -m scripts.sync_tenants --force             # push even if TENANT_SOURCE=supabase
    python -m scripts.sync_tenants --export             # pull Supabase -> JSON instead

Whether "the JSON files" or "Supabase" is the bot's actual source of truth
depends entirely on `TENANT_SOURCE` (`.env`). With the Phase 4 default
(`json`), pushing here is just bookkeeping — nothing reads these tables yet,
so re-running any time a tenant JSON changes is always safe. Once the Phase 8
admin panel is live (`TENANT_SOURCE=supabase`), Supabase *is* the running
app's truth, and a blind push from disk would silently revert whatever an
operator just changed through the panel — "the sync stomp"
(`plans/phase8.md`). `--force` is what makes overwriting live config with
what's on disk a deliberate choice instead of a habit; `--export` goes the
other direction, when the JSON files are the ones that are stale.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from rich.console import Console

from app.config import get_settings
from app.logging_config import configure_logging, force_utf8_console
from app.tenancy.loader import get_repository, get_tenant_config
from app.tenancy.sync import TenantSyncError, _admin_client, sync_tenant

console = Console()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", help="sync only this tenant_id (default: every tenant)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="push disk -> Supabase even when TENANT_SOURCE=supabase (the running app's "
        "actual truth) — without this flag that combination refuses to run",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="pull Supabase -> JSON instead of the other direction, overwriting "
        "content/tenants/<id>.json with what's currently live",
    )
    return parser.parse_args(argv)


async def _push(tenant_ids: list[str]) -> int:
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


async def _export(tenant_ids: list[str]) -> int:
    # Reuses the exact hydration app/tenancy/supabase_repository.py's boot-time
    # snapshot uses (round-trip fidelity is what tests/test_supabase_tenant_repository.py
    # proves), so an exported file and what the running app actually serves in
    # TENANT_SOURCE=supabase mode can never silently disagree in shape.
    from app.tenancy.supabase_repository import _row_to_tenant_config

    settings = get_settings()
    client = _admin_client(settings, timeout=settings.supabase_timeout_seconds)
    try:
        response = await client.get("/tenants", params={"select": "*,services(*)"})
    finally:
        await client.aclose()

    if response.status_code >= 400:
        console.print(f"[bold red]error:[/bold red] GET /tenants -> {response.status_code}")
        return 1

    rows = {row["tenant_id"]: row for row in response.json()}
    failures = 0
    for tenant_id in tenant_ids:
        row = rows.get(tenant_id)
        if row is None:
            console.print(f"[bold red]✗ {tenant_id}:[/bold red] not found in Supabase")
            failures += 1
            continue
        try:
            config = _row_to_tenant_config(row)
        except Exception as exc:  # noqa: BLE001 — report and move on to the next tenant
            console.print(f"[bold red]✗ {tenant_id}:[/bold red] failed to validate: {exc}")
            failures += 1
            continue
        path = settings.tenant_data_dir / f"{tenant_id}.json"
        path.write_text(
            json.dumps(config.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        console.print(f"[green]✓[/green] {tenant_id} -> {path}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    force_utf8_console()
    configure_logging()
    args = _parse_args(argv)
    settings = get_settings()

    if not settings.supabase_url or not settings.supabase_secret_key:
        console.print("[bold red]error:[/bold red] SUPABASE_URL / SUPABASE_SECRET_KEY must be set")
        return 1

    if args.export:
        tenant_ids = [args.tenant] if args.tenant else get_repository().list_ids()
        if not tenant_ids:
            console.print("[dim]no tenants found[/dim]")
            return 0
        return asyncio.run(_export(tenant_ids))

    if settings.tenant_source == "supabase" and not args.force:
        console.print(
            "[bold red]error:[/bold red] TENANT_SOURCE=supabase — the running app already "
            "treats Supabase as the source of truth, so pushing from disk would silently "
            'overwrite any edit made through the admin panel ("the sync stomp", '
            "plans/phase8.md). Pass --force if you genuinely mean to push disk -> Supabase, "
            "or --export to pull the other direction first."
        )
        return 1

    try:
        tenant_ids = [args.tenant] if args.tenant else get_repository().list_ids()
    except LookupError as exc:
        console.print(f"[bold red]error:[/bold red] {exc}")
        return 1

    if not tenant_ids:
        console.print("[dim]no tenants found[/dim]")
        return 0

    return asyncio.run(_push(tenant_ids))


if __name__ == "__main__":
    sys.exit(main())
