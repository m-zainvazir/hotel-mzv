"""Create or update a tenant's Vapi assistant — and switch how it's reachable.

    python -m scripts.provision_vapi --tenant hotel-mzv              # web call only
    python -m scripts.provision_vapi --tenant hotel-mzv --dry-run    # print, don't send
    python -m scripts.provision_vapi --tenant hotel-mzv --attach-number +15551230000
    python -m scripts.provision_vapi --tenant hotel-mzv --detach-number
    python -m scripts.provision_vapi --tenant hotel-mzv --show

Re-run it freely: it updates the existing assistant rather than creating a new
one, which you'll need every time an ngrok URL rotates.

The web call and the phone call are the same assistant. Attaching a number adds
PSTN; detaching leaves the web call working. Typed chat (`scripts/chat_cli.py`,
`POST /chat`) is unaffected by any of this.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from rich.console import Console
from rich.panel import Panel

from app.channels.vapi_provisioning import (
    ProvisioningError,
    VapiClient,
    build_assistant_payload,
    save_vapi_ids,
)
from app.config import get_settings
from app.logging_config import configure_logging, force_utf8_console
from app.tenancy.loader import clear_tenant_cache, get_repository, get_tenant_config, set_repository
from app.tenancy.supabase_repository import SupabaseTenantRepository

console = Console()


async def _init_repository() -> None:
    """Mirror app/main.py's lifespan: without this, a panel-created tenant
    (Supabase-only, no content/tenants/<id>.json — Phase 9 Part B) is
    invisible here regardless of TENANT_SOURCE, since get_tenant_config
    otherwise defaults to the JSON-file repository unconditionally. Same fix
    as scripts/chat_cli.py's own _init_repository — found live (2026-08-03)
    the moment a second script needed to resolve a panel-only tenant."""
    if get_settings().tenant_source == "supabase":
        repo = SupabaseTenantRepository(fallback=get_repository())
        await repo.refresh()
        set_repository(repo)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", required=True, help="tenant_id to provision")
    parser.add_argument("--attach-number", metavar="E164", help="import/attach a phone number")
    parser.add_argument("--area-code", help="area code when Vapi provisions the number")
    parser.add_argument("--detach-number", action="store_true", help="go back to web-call only")
    parser.add_argument("--show", action="store_true", help="print current wiring and exit")
    parser.add_argument("--dry-run", action="store_true", help="print the payload, send nothing")
    return parser.parse_args(argv)


def _show(tenant, settings) -> None:
    # Bracketed placeholders must be rich markup, or rich silently eats them.
    unset = "[dim]not set[/dim]"
    base = (settings.public_base_url or "").rstrip("/")
    assistant = tenant.vapi.assistant_id or "[dim]not provisioned[/dim]"
    number = tenant.vapi.phone_number_id or "[dim]none — web call only[/dim]"
    voice_id = tenant.voice.voice_id or settings.cartesia_default_voice_id or "[dim]unset[/dim]"
    if tenant.emergency.escalation_phone and tenant.emergency.allow_warm_transfer:
        transfer = f"enabled -> {tenant.emergency.escalation_phone}"
    else:
        transfer = "[dim]disabled (allow_warm_transfer=false or no escalation_phone)[/dim]"
    console.print(
        Panel(
            f"[bold]{tenant.name}[/bold]  ({tenant.tenant_id})\n\n"
            f"assistant_id    : {assistant}\n"
            f"phone_number_id : {number}\n"
            f"voice           : {tenant.voice.provider} / {voice_id}\n"
            f"warm transfer   : {transfer}\n"
            f"custom-llm url  : {base or unset}\n"
            f"webhook url     : {f'{base}/webhooks/vapi' if base else unset}\n"
            f"webhook secret  : {'set' if settings.vapi_webhook_secret else '[dim]unset[/dim]'}",
            title="Vapi wiring",
            border_style="cyan",
        )
    )


def main(argv: list[str] | None = None) -> int:
    force_utf8_console()
    configure_logging()
    args = _parse_args(argv)
    settings = get_settings()
    asyncio.run(_init_repository())

    try:
        tenant = get_tenant_config(args.tenant)
    except LookupError as exc:
        console.print(f"[bold red]error:[/bold red] {exc}")
        return 1

    if args.show:
        _show(tenant, settings)
        return 0

    try:
        payload = build_assistant_payload(tenant, settings=settings)
    except ProvisioningError as exc:
        console.print(f"[bold red]error:[/bold red] {exc}")
        return 1

    if args.dry_run:
        console.print_json(json.dumps(payload))
        return 0

    try:
        with VapiClient(settings) as client:
            assistant = client.upsert_assistant(payload, tenant.vapi.assistant_id)
            assistant_id = assistant.get("id") or tenant.vapi.assistant_id
            if not assistant_id:
                console.print("[bold red]error:[/bold red] Vapi returned no assistant id")
                return 1

            save_vapi_ids(
                tenant.tenant_id,
                data_dir=settings.tenant_data_dir,
                assistant_id=assistant_id,
                tenant=tenant,
            )
            # Keep the in-memory tenant in sync with what was just persisted —
            # the phone-number save below (if it fires) must build from the
            # assistant_id that was just set, not the pre-provisioning state,
            # regardless of whether that write lands in a JSON file or Supabase.
            tenant = tenant.model_copy(
                update={"vapi": tenant.vapi.model_copy(update={"assistant_id": assistant_id})}
            )
            console.print(f"[green]✓[/green] assistant [bold]{assistant_id}[/bold] up to date")

            phone_number_id: str | None | type(...) = ...  # type: ignore[valid-type]

            if args.detach_number:
                if tenant.vapi.phone_number_id:
                    client.attach_assistant_to_number(tenant.vapi.phone_number_id, None)
                phone_number_id = None
                console.print("[green]✓[/green] number detached — web call only")

            elif args.attach_number:
                if settings.twilio_account_sid and settings.twilio_auth_token:
                    number = client.import_twilio_number(
                        args.attach_number,
                        assistant_id,
                        settings.twilio_account_sid,
                        settings.twilio_auth_token,
                    )
                else:
                    console.print("[dim]no Twilio credentials — asking Vapi for a number[/dim]")
                    number = client.create_vapi_number(assistant_id, args.area_code)
                phone_number_id = number.get("id")
                console.print(
                    f"[green]✓[/green] number [bold]{number.get('number', '?')}[/bold] "
                    f"attached ({phone_number_id})"
                )

            if phone_number_id is not ...:
                save_vapi_ids(
                    tenant.tenant_id,
                    data_dir=settings.tenant_data_dir,
                    phone_number_id=phone_number_id,
                    tenant=tenant,
                )

    except ProvisioningError as exc:
        console.print(f"[bold red]vapi error:[/bold red] {exc}")
        return 1

    clear_tenant_cache()
    _show(get_tenant_config(args.tenant), settings)
    console.print(
        "\n[dim]Next: talk to it from the Vapi dashboard (web call), or dial the number "
        "if you attached one. Keep `uvicorn app.main:app` and your tunnel running.[/dim]"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
