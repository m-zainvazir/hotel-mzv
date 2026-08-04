"""Terminal chat with the brain — the Phase 1 acceptance test you can drive by hand.

    python -m scripts.chat_cli
    python -m scripts.chat_cli --tenant northside-plumbing --channel voice

Tokens print as the model produces them; tool calls show up dimmed so you can
watch the acknowledge-then-act rule working. `/jobs` prints what got booked.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from rich.console import Console
from rich.panel import Panel

from app.brain.runner import stream_turn
from app.config import get_settings
from app.db.factory import get_store
from app.logging_config import configure_logging, force_utf8_console
from app.tenancy.loader import get_repository, get_tenant_config, resolve_tenant_id, set_repository
from app.tenancy.supabase_repository import SupabaseTenantRepository
from app.tools.formatting import speakable_datetime

console = Console()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chat with the AI receptionist.")
    parser.add_argument("--tenant", default=None, help="tenant_id (default: DEFAULT_TENANT_ID)")
    parser.add_argument("--channel", default="chat", choices=["chat", "voice"])
    parser.add_argument("--session", default=None, help="conversation id (default: random)")
    parser.add_argument("--show-tools", action="store_true", help="print tool results in full")
    return parser.parse_args(argv)


async def _print_jobs(tenant_id: str) -> None:
    tenant = get_tenant_config(tenant_id)
    jobs = await get_store().alist_jobs(tenant_id)
    if not jobs:
        console.print("[dim]No jobs booked yet.[/dim]")
        return
    for job in jobs:
        console.print(
            f"  [bold]{job.id}[/bold]  {job.service_name} — {job.customer_name} "
            f"({job.customer_phone})\n"
            f"     {speakable_datetime(job.scheduled_start, tenant.tz)} @ {job.address} "
            f"[{job.status.value}]"
        )
    for message in await get_store().alist_messages(tenant_id):
        console.print(f"  [dim]sms → {message.to}: {message.body}[/dim]")


async def _init_repository() -> None:
    """Mirror app/main.py's lifespan: without this, a panel-created tenant
    (Supabase-only, no content/tenants/<id>.json — Phase 9 Part B) is
    invisible here regardless of TENANT_SOURCE, since get_tenant_config
    otherwise defaults to the JSON-file repository unconditionally."""
    if get_settings().tenant_source == "supabase":
        repo = SupabaseTenantRepository(fallback=get_repository())
        await repo.refresh()
        set_repository(repo)


async def converse(args: argparse.Namespace) -> None:
    await _init_repository()
    tenant_id = resolve_tenant_id(tenant_id=args.tenant)
    tenant = get_tenant_config(tenant_id)
    session_id = args.session or f"cli-{uuid.uuid4().hex[:6]}"

    settings = get_settings()
    console.print(
        Panel(
            f"[bold]{tenant.name}[/bold] — {tenant.trade} · {tenant.timezone}\n"
            f"channel=[cyan]{args.channel}[/cyan]  session=[cyan]{session_id}[/cyan]\n"
            f"model=[magenta]{settings.llm_provider}/{settings.active_model}[/magenta]\n\n"
            f"[italic]{tenant.greeting}[/italic]",
            title="AI Receptionist",
            border_style="cyan",
        )
    )
    console.print("[dim]Commands: /jobs  /reset  /quit[/dim]\n")

    while True:
        try:
            user = console.input("[bold green]you ›[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return

        if not user:
            continue
        if user in ("/quit", "/exit"):
            return
        if user == "/jobs":
            await _print_jobs(tenant_id)
            continue
        if user == "/reset":
            session_id = f"cli-{uuid.uuid4().hex[:6]}"
            console.print(f"[dim]new session {session_id}[/dim]")
            continue

        console.print(f"[bold cyan]{tenant.name} ›[/bold cyan] ", end="")
        wrote_anything = False

        async for event in stream_turn(
            text=user,
            tenant_id=tenant_id,
            session_id=session_id,
            channel=args.channel,
        ):
            if event.type in ("token", "acknowledgement"):
                console.print(event.text, end="", style="" if event.type == "token" else "italic")
                wrote_anything = True
            elif event.type == "tool_start":
                console.print(f"\n[dim]  ⚙ {event.tool}({_brief(event.data)})[/dim]")
            elif event.type == "tool_result" and args.show_tools:
                console.print(f"[dim]  ↳ {event.text}[/dim]")
            elif event.type == "error":
                console.print(f"\n[bold red]error:[/bold red] {event.text}")
                wrote_anything = True

        console.print("\n" if wrote_anything else "[dim](no reply)[/dim]\n")


def _brief(data: dict) -> str:
    parts = []
    for key, value in list(data.items())[:3]:
        text = str(value)
        parts.append(f"{key}={text[:28] + '…' if len(text) > 28 else text}")
    return ", ".join(parts)


def main(argv: list[str] | None = None) -> int:
    force_utf8_console()
    configure_logging()
    args = _parse_args(argv)
    try:
        asyncio.run(converse(args))
    except KeyboardInterrupt:  # pragma: no cover
        pass
    console.print("[dim]bye.[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
