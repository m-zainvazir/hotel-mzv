"""Onboard a new tenant — the "add a client" button (plan §6d, Phase 4 Step 9).

    python -m scripts.onboard_tenant --config new-tenant.json --dry-run
    python -m scripts.onboard_tenant --config new-tenant.json
    python -m scripts.onboard_tenant --config new-tenant.json \\
        --calcom-api-key cal_live_... \\
        --twilio-account-sid AC... --twilio-auth-token ... \\
        --voice-sample sample.wav --consent-url https://.../consent.pdf \\
        --consent-owner "Jane Doe" --consent-granted-by "jane@example.com" \\
        --provision-vapi

`--config` is a JSON file already shaped like a tenant profile — see any file
in content/tenants/ for the shape (`greeting`, `hours`, `services`, ... same
schema `TenantConfig` validates). Idempotent and re-runnable, like
`provision_vapi.py`: re-running with the same `--config` updates the
existing tenant rather than creating a duplicate.

What this does, in order (plan §6d):
  1. Validate --config, write content/tenants/<id>.json.
  2. Sync the tenants + services rows into Supabase (app/tenancy/sync.py).
  3. --calcom-api-key / --twilio-*: write per-tenant secrets into Vault
     (app/tenancy/secrets.py — unblocks a tenant sharing nobody else's
     Cal.com/Twilio account).
  4. --voice-sample + --consent-*: record consent, clone the voice, write
     the resulting voice_id back into the tenant JSON + Supabase.
  5. --provision-vapi: create/update the Vapi assistant — delegates to
     scripts.provision_vapi, not duplicated here.
  6. Flip status -> "active".

Two plan §6d steps are deliberately not automated here:
  * Calendar connection (Cal.com) — event types are still created by hand in
    the Cal.com dashboard (see content/README.md's checklist); this script
    only handles the credential half (--calcom-api-key). Put the resulting
    event_type_id in --config's booking.event_type_id yourself.
  * MCP server registration — Phase 6 isn't built yet. mcp_servers in
    --config round-trips into the tenant file and Supabase; nothing
    consumes it until Phase 6 lands.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from rich.console import Console

from app.config import get_settings
from app.logging_config import configure_logging, force_utf8_console
from app.tenancy.loader import clear_tenant_cache
from app.tenancy.models import TenantConfig
from app.tenancy.secrets import TenantSecretError, set_tenant_secret
from app.tenancy.sync import TenantSyncError, sync_tenant
from app.tenancy.voice import CartesiaError, VoiceConsentError, clone_voice, record_consent

console = Console()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="path to a TenantConfig-shaped JSON file")
    parser.add_argument("--calcom-api-key", help="per-tenant Cal.com API key -> Vault")
    parser.add_argument("--twilio-account-sid", help="per-tenant Twilio SID -> Vault")
    parser.add_argument("--twilio-auth-token", help="per-tenant Twilio auth token -> Vault")
    parser.add_argument("--voice-sample", help="path to a consented audio sample to clone")
    parser.add_argument("--consent-url", help="URL/location of the signed consent artifact")
    parser.add_argument("--consent-owner", help="name of the person whose voice this is")
    parser.add_argument("--consent-granted-by", help="who is granting consent, on record")
    parser.add_argument(
        "--provision-vapi", action="store_true", help="also create/update the Vapi assistant"
    )
    parser.add_argument("--dry-run", action="store_true", help="validate and print, write nothing")
    return parser.parse_args(argv)


def _load_config(path: str) -> TenantConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return TenantConfig.model_validate(raw)


def _write_json(tenant: TenantConfig) -> Path:
    dest = get_settings().tenant_data_dir / f"{tenant.tenant_id}.json"
    dest.write_text(tenant.model_dump_json(indent=2) + "\n", encoding="utf-8")
    clear_tenant_cache()
    return dest


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()

    try:
        tenant = _load_config(args.config)
    except (OSError, ValueError) as exc:
        console.print(f"[bold red]invalid config:[/bold red] {exc}")
        return 1

    console.print(f"onboarding [bold]{tenant.tenant_id}[/bold] ({tenant.name})")

    if args.dry_run:
        console.print_json(tenant.model_dump_json())
        return 0

    if not settings.supabase_url or not settings.supabase_secret_key:
        console.print("[bold red]error:[/bold red] SUPABASE_URL / SUPABASE_SECRET_KEY must be set")
        return 1

    # 1. write the JSON file — onboarding starts in "onboarding" status
    #    regardless of what --config says, so a half-finished run never
    #    looks live by accident.
    tenant = tenant.model_copy(update={"status": "onboarding"})
    dest = _write_json(tenant)
    console.print(f"[green]✓[/green] wrote {dest}")

    # 2. sync to Supabase
    try:
        await sync_tenant(tenant)
        console.print("[green]✓[/green] synced to Supabase")
    except TenantSyncError as exc:
        console.print(f"[bold red]✗ Supabase sync failed:[/bold red] {exc}")
        return 1

    # 3. per-tenant secrets
    for key_name, value in (
        ("calcom_api_key", args.calcom_api_key),
        ("twilio_account_sid", args.twilio_account_sid),
        ("twilio_auth_token", args.twilio_auth_token),
    ):
        if not value:
            continue
        try:
            await set_tenant_secret(tenant.tenant_id, key_name, value)
            console.print(f"[green]✓[/green] {key_name} stored in Vault")
        except TenantSecretError as exc:
            console.print(f"[bold red]✗ could not store {key_name}:[/bold red] {exc}")
            return 1

    # 4. voice consent + clone
    if args.voice_sample:
        if not (args.consent_url and args.consent_owner and args.consent_granted_by):
            console.print(
                "[bold red]error:[/bold red] --voice-sample needs --consent-url, "
                "--consent-owner and --consent-granted-by — no exceptions (CLAUDE.md #6)"
            )
            return 1
        try:
            await record_consent(
                tenant_id=tenant.tenant_id,
                voice_owner_name=args.consent_owner,
                consent_artifact_url=args.consent_url,
                granted_by=args.consent_granted_by,
            )
            console.print("[green]✓[/green] consent recorded")
            voice_id = await clone_voice(
                tenant_id=tenant.tenant_id,
                audio_path=args.voice_sample,
                voice_name=f"{tenant.name} receptionist",
            )
            console.print(f"[green]✓[/green] voice cloned -> [bold]{voice_id}[/bold]")
        except (VoiceConsentError, CartesiaError) as exc:
            console.print(f"[bold red]✗ voice clone failed:[/bold red] {exc}")
            return 1

        tenant = tenant.model_copy(
            update={"voice": tenant.voice.model_copy(update={"voice_id": voice_id})}
        )
        _write_json(tenant)
        try:
            await sync_tenant(tenant)
        except TenantSyncError as exc:
            console.print(f"[bold red]✗ could not sync voice_id:[/bold red] {exc}")
            return 1
        console.print("[green]✓[/green] voice_id written to JSON + Supabase")

    # 5. Vapi assistant — delegate to provision_vapi, not duplicated here.
    if args.provision_vapi:
        from scripts.provision_vapi import main as provision_vapi_main

        code = provision_vapi_main(["--tenant", tenant.tenant_id])
        if code != 0:
            console.print("[bold red]✗ provision_vapi failed[/bold red]")
            return code

    # 6. flip status -> active
    tenant = tenant.model_copy(update={"status": "active"})
    _write_json(tenant)
    try:
        await sync_tenant(tenant)
    except TenantSyncError as exc:
        console.print(f"[bold red]✗ could not flip status to active:[/bold red] {exc}")
        return 1

    console.print(f"\n[bold green]✓ {tenant.tenant_id} is now active[/bold green]")
    if not args.provision_vapi:
        console.print(
            f"[dim]Next: python -m scripts.provision_vapi --tenant {tenant.tenant_id}[/dim]"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    force_utf8_console()
    configure_logging()
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
