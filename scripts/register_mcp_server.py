"""Connect (or disconnect) one MCP server for one tenant — the "MCP_SOURCE=
supabase" half of Phase 6, and the command that makes "connect literally any
remote MCP server" actually mean "no redeploy".

    python -m scripts.register_mcp_server --tenant hotel-mzv --name tavily \\
        --url 'https://mcp.tavily.com/mcp/?tavilyApiKey=${secret}' \\
        --secret tvly-xxxxx
    python -m scripts.register_mcp_server --tenant hotel-mzv --name acme_crm \\
        --url 'https://acme.example/mcp' \\
        --header 'Authorization: Bearer ${secret}' --header 'X-Acme-Org: hotel-mzv' \\
        --secret sk_live_... --tool-allowlist search,extract

    python -m scripts.register_mcp_server --tenant hotel-mzv --list
    python -m scripts.register_mcp_server --tenant hotel-mzv --name tavily --disable
    python -m scripts.register_mcp_server --tenant hotel-mzv --name tavily --enable
    python -m scripts.register_mcp_server --tenant hotel-mzv --name tavily --remove

Idempotent and re-runnable, like `provision_vapi.py` / `onboard_tenant.py`: a
second run with the same `--name` updates the row rather than creating a
duplicate (`on_conflict=tenant_id,name`).

Writes go through the Supabase **secret** key (admin path, bypasses RLS) —
registering a server is a backend-operator action, not a tenant reading its
own data, the same reasoning `app/tenancy/sync.py` documents. `--secret`, if
given, is written into Vault via the existing `set_tenant_secret` — never
inlined into the row itself; the row only ever carries a `${secret}`
placeholder plus a reference to where the real value lives
(`app/mcp/connections.py` does the substitution at connect time).

This intentionally does NOT touch `content/tenants/<id>.json`. The whole
point of `MCP_SOURCE=supabase` is that adding a server to a live tenant is
one row insert, not an edit-commit-redeploy — see `app/config.py`'s
`mcp_source` docstring. `scripts/onboard_tenant.py` / `sync_tenants.py`
remain the way a JSON-authored server list reaches Supabase for
`MCP_SOURCE=json` dev/test parity.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import httpx
from rich.console import Console
from rich.table import Table

from app.config import get_settings
from app.logging_config import configure_logging, force_utf8_console
from app.mcp.connections import redacted
from app.tenancy.models import McpServerConfig
from app.tenancy.secrets import TenantSecretError, set_tenant_secret

console = Console()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", required=True, help="tenant_id")
    parser.add_argument("--name", help="server name (required unless --list)")
    parser.add_argument("--url", help="server URL — http transport")
    parser.add_argument("--transport", choices=["http", "stdio"], default="http")
    parser.add_argument("--command", help="stdio transport: executable to run")
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        dest="command_args",
        help="stdio transport: one positional argument, repeatable",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="extra header as 'Key: Value', repeatable — may contain literal "
        "${secret}, substituted from --secret at connect time",
    )
    parser.add_argument(
        "--secret", help="credential value, written to Vault — never inlined into the row"
    )
    parser.add_argument("--secret-key", help="Vault key name for --secret (default: mcp_<name>)")
    parser.add_argument(
        "--tool-allowlist",
        help="comma-separated tool names; omit for 'every tool this server offers'",
    )
    parser.add_argument("--disable", action="store_true", help="register/mark disabled")
    parser.add_argument("--enable", action="store_true", help="mark an existing server enabled")
    parser.add_argument("--remove", action="store_true", help="delete the row entirely")
    parser.add_argument("--list", action="store_true", help="list this tenant's servers")
    parser.add_argument("--dry-run", action="store_true", help="print, write nothing")
    return parser.parse_args(argv)


def _admin_client(settings, *, timeout: float) -> httpx.AsyncClient:
    # Mirrors app/tenancy/sync.py's _admin_client exactly — same admin path,
    # same reasoning: this is a backend-operator action, not a tenant reading
    # its own data, so it uses the Supabase secret key rather than a
    # tenant-scoped JWT.
    return httpx.AsyncClient(
        base_url=f"{settings.supabase_url}/rest/v1",
        headers={
            "apikey": settings.supabase_secret_key or "",
            "Authorization": f"Bearer {settings.supabase_secret_key}",
            "Prefer": "resolution=merge-duplicates,return=representation",
        },
        timeout=timeout,
    )


def _parse_headers(raw: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw:
        key, sep, value = item.partition(":")
        if not sep:
            raise ValueError(f"--header {item!r} must look like 'Key: Value'")
        headers[key.strip()] = value.strip()
    return headers


async def _list(tenant_id: str) -> int:
    settings = get_settings()
    async with _admin_client(settings, timeout=settings.supabase_timeout_seconds) as client:
        response = await client.get("/mcp_servers", params={"tenant_id": f"eq.{tenant_id}"})
    if response.status_code >= 400:
        console.print(f"[bold red]error:[/bold red] {response.status_code} {response.text[:300]}")
        return 1

    rows = response.json() if response.content else []
    if not rows:
        console.print(f"[dim]no MCP servers registered for {tenant_id}[/dim]")
        return 0

    table = Table(title=f"MCP servers — {tenant_id}")
    for col in ("name", "enabled", "transport", "url", "tool_allowlist"):
        table.add_column(col)
    for row in rows:
        safe = redacted({"url": row.get("url") or "", "headers": row.get("headers") or {}})
        table.add_row(
            row["name"],
            str(row.get("enabled", True)),
            row.get("transport", "http"),
            safe["url"] if row.get("url") else (row.get("command") or ""),
            ", ".join(row.get("tool_allowlist") or []) or "(all)",
        )
    console.print(table)
    return 0


async def _remove(tenant_id: str, name: str, *, dry_run: bool) -> int:
    if dry_run:
        console.print(f"[dim]would delete {tenant_id}/{name}[/dim]")
        return 0
    settings = get_settings()
    async with _admin_client(settings, timeout=settings.supabase_timeout_seconds) as client:
        response = await client.delete(
            "/mcp_servers", params={"tenant_id": f"eq.{tenant_id}", "name": f"eq.{name}"}
        )
    if response.status_code >= 400:
        console.print(f"[bold red]error:[/bold red] {response.status_code} {response.text[:300]}")
        return 1
    console.print(f"[green]✓[/green] removed {tenant_id}/{name}")
    return 0


async def _toggle(tenant_id: str, name: str, *, enabled: bool, dry_run: bool) -> int:
    if dry_run:
        console.print(f"[dim]would set {tenant_id}/{name} enabled={enabled}[/dim]")
        return 0
    settings = get_settings()
    async with _admin_client(settings, timeout=settings.supabase_timeout_seconds) as client:
        response = await client.patch(
            "/mcp_servers",
            params={"tenant_id": f"eq.{tenant_id}", "name": f"eq.{name}"},
            json={"enabled": enabled},
        )
    if response.status_code >= 400:
        console.print(f"[bold red]error:[/bold red] {response.status_code} {response.text[:300]}")
        return 1
    if response.json() == []:
        console.print(f"[bold red]error:[/bold red] no row {tenant_id}/{name} to update")
        return 1
    console.print(f"[green]✓[/green] {tenant_id}/{name} enabled={enabled}")
    return 0


async def _register(args: argparse.Namespace) -> int:
    headers = _parse_headers(args.header)
    secret_key = args.secret_key or f"mcp_{args.name}"
    allowlist = [t.strip() for t in (args.tool_allowlist or "").split(",") if t.strip()]

    # Validate the shape before writing anything — the same load-bearing
    # checks TenantConfig applies to a JSON-authored server (illegal name
    # charset, http-without-url, stdio-without-command) apply here too, and
    # failing locally beats a confusing PostgREST 400.
    try:
        McpServerConfig(
            name=args.name,
            enabled=not args.disable,
            transport=args.transport,
            url=args.url,
            headers=headers,
            tool_allowlist=allowlist,
            command=args.command,
            args=args.command_args,
            auth_secret_ref=secret_key if args.secret else None,
        )
    except Exception as exc:
        console.print(f"[bold red]invalid server config:[/bold red] {exc}")
        return 1

    row: dict[str, Any] = {
        "tenant_id": args.tenant,
        "name": args.name,
        "enabled": not args.disable,
        "transport": args.transport,
        "url": args.url,
        "headers": headers,
        "tool_allowlist": allowlist,
        "command": args.command,
        "args": args.command_args,
        "auth_secret_ref": secret_key if args.secret else None,
    }

    if args.dry_run:
        console.print_json(data=redacted(dict(row)) if row.get("url") else row)
        return 0

    settings = get_settings()

    # Secret first: a row referencing auth_secret_ref should never point at a
    # Vault entry that doesn't exist yet, even transiently.
    if args.secret:
        try:
            await set_tenant_secret(args.tenant, secret_key, args.secret)
            console.print(f"[green]✓[/green] secret stored in Vault as {secret_key!r}")
        except TenantSecretError as exc:
            console.print(f"[bold red]✗ could not store secret:[/bold red] {exc}")
            return 1

    async with _admin_client(settings, timeout=settings.supabase_timeout_seconds) as client:
        response = await client.post(
            "/mcp_servers", params={"on_conflict": "tenant_id,name"}, json=row
        )
    if response.status_code >= 400:
        console.print(
            f"[bold red]✗ could not write row:[/bold red] "
            f"{response.status_code} {response.text[:300]}"
        )
        return 1

    console.print(
        f"[green]✓[/green] {args.tenant}/{args.name} registered "
        f"(MCP_SOURCE=supabase to have the brain use it without a redeploy)"
    )
    return 0


async def _run(args: argparse.Namespace) -> int:
    if not args.name and not args.list:
        console.print("[bold red]error:[/bold red] --name is required unless --list")
        return 1

    has_endpoint = bool(args.url or args.command)
    # --disable/--enable with no --url/--command is a pure toggle on an
    # existing row; with one, it's the initial enabled state of a fresh
    # registration — the two must not be conflated.
    is_toggle_only = (args.disable or args.enable) and not has_endpoint
    is_registering = not (args.list or args.remove or is_toggle_only)

    # --dry-run only skips the Supabase requirement for a register — --list,
    # --remove and the toggle are inherently live actions.
    if not (args.dry_run and is_registering):
        settings = get_settings()
        if not settings.supabase_url or not settings.supabase_secret_key:
            console.print(
                "[bold red]error:[/bold red] SUPABASE_URL / SUPABASE_SECRET_KEY must be set"
            )
            return 1

    if args.list:
        return await _list(args.tenant)

    if args.remove:
        return await _remove(args.tenant, args.name, dry_run=args.dry_run)

    if is_toggle_only:
        return await _toggle(args.tenant, args.name, enabled=args.enable, dry_run=args.dry_run)

    if not has_endpoint:
        console.print(
            "[bold red]error:[/bold red] --url (http) or --command (stdio) is required "
            "to register a server"
        )
        return 1

    return await _register(args)


def main(argv: list[str] | None = None) -> int:
    force_utf8_console()
    configure_logging()
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
