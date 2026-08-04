"""One-time interactive Cal.com MCP authorization for one tenant (Phase 9 Part A).

    python -m scripts.authorize_calcom --tenant hotel-mzv

Opens the tenant's own Cal.com account in a browser, walks the OAuth 2.1
authorization-code + PKCE grant (RFC 7636) against Cal.com's hosted MCP
server (`https://mcp.cal.com` — discovery, DCR and refresh-token support all
confirmed live; see `app/mcp/oauth.py`'s module docstring for the Step A0
spike this is built from), and stores the resulting refresh token + client
credentials in Supabase Vault, scoped to that tenant, via `set_tenant_secret`.
Re-runnable: a second run registers a fresh DCR client and overwrites the
stored grant, same posture as `provision_vapi.py` / `onboard_tenant.py`.

After this succeeds, flip the tenant's `booking.provider` to `"mcp_calcom"`
in its config — see `content/README.md`'s "Booking via MCP" section.
Headless refresh from here on is `app/mcp/oauth.py::access_token_for`,
called by `app/tools/booking/mcp_calcom.py` on every turn; this script never
runs again unless the grant is revoked.
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import secrets
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from rich.console import Console

from app.config import get_settings
from app.logging_config import configure_logging, force_utf8_console
from app.mcp.oauth import (
    CLIENT_ID_KEY,
    CLIENT_SECRET_KEY,
    REFRESH_TOKEN_KEY,
    CalcomOAuthError,
    build_authorize_url,
    discover,
    exchange_code,
    generate_pkce_pair,
    register_client,
)
from app.tenancy.secrets import TenantSecretError, set_tenant_secret

console = Console()

_CALLBACK_TIMEOUT_SECONDS = 300.0


@dataclass
class _CallbackResult:
    code: str | None = None
    state: str | None = None
    error: str | None = None
    event: threading.Event = field(default_factory=threading.Event)


def _make_handler(result: _CallbackResult) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:  # silence default stderr access log
            pass

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler signature
            query = parse_qs(urlsplit(self.path).query)
            result.code = (query.get("code") or [None])[0]
            result.state = (query.get("state") or [None])[0]
            result.error = (query.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            body = (
                f"<html><body><p>Authorization failed: {result.error}</p></body></html>"
                if result.error
                else "<html><body><p>Cal.com authorization complete — you can close this "
                "tab and return to the terminal.</p></body></html>"
            )
            self.wfile.write(body.encode("utf-8"))
            result.event.set()

    return Handler


def _await_callback(port: int, result: _CallbackResult) -> None:
    server = http.server.HTTPServer(("127.0.0.1", port), _make_handler(result))
    server.timeout = _CALLBACK_TIMEOUT_SECONDS
    server.handle_request()
    server.server_close()


async def _run(tenant_id: str, *, port: int, scope: str | None, open_browser: bool) -> int:
    settings = get_settings()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    console.print(f"[dim]discovering OAuth metadata at {settings.calcom_mcp_url}...[/dim]")
    try:
        metadata = await discover(settings.calcom_mcp_url)
    except CalcomOAuthError as exc:
        console.print(f"[bold red]discovery failed:[/bold red] {exc}")
        return 1

    console.print("[dim]registering a client (Dynamic Client Registration)...[/dim]")
    try:
        registration = await register_client(
            metadata, redirect_uri=redirect_uri, client_name=f"ai-receptionist-{tenant_id}"
        )
    except CalcomOAuthError as exc:
        console.print(f"[bold red]client registration failed:[/bold red] {exc}")
        return 1

    pkce = generate_pkce_pair()
    state = secrets.token_urlsafe(24)
    authorize_url = build_authorize_url(
        metadata,
        client_id=registration.client_id,
        redirect_uri=redirect_uri,
        code_challenge=pkce.challenge,
        state=state,
        scope=scope,
    )

    result = _CallbackResult()
    listener = threading.Thread(target=_await_callback, args=(port, result), daemon=True)
    listener.start()

    console.print(
        f"\nOpen this URL and sign into [bold]{tenant_id}[/bold]'s own Cal.com account:\n"
    )
    console.print(f"  {authorize_url}\n")
    if open_browser:
        webbrowser.open(authorize_url)
    console.print(
        f"[dim]waiting for the callback ({int(_CALLBACK_TIMEOUT_SECONDS)}s timeout)...[/dim]"
    )

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, result.event.wait, _CALLBACK_TIMEOUT_SECONDS)
    listener.join(timeout=1)

    if result.error:
        console.print(f"[bold red]authorization denied:[/bold red] {result.error}")
        return 1
    if not result.code:
        console.print("[bold red]timed out waiting for the browser callback[/bold red]")
        return 1
    if result.state != state:
        console.print("[bold red]state mismatch — possible CSRF, aborting[/bold red]")
        return 1

    console.print("[dim]exchanging the authorization code...[/dim]")
    try:
        tokens = await exchange_code(
            metadata,
            code=result.code,
            redirect_uri=redirect_uri,
            client_id=registration.client_id,
            client_secret=registration.client_secret,
            code_verifier=pkce.verifier,
        )
    except CalcomOAuthError as exc:
        console.print(f"[bold red]token exchange failed:[/bold red] {exc}")
        return 1

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        console.print(
            "[bold red]no refresh_token in the token response[/bold red] — everything "
            "headless in app/mcp/oauth.py depends on one; aborting rather than writing a "
            "grant that can't outlive the access token's own short expiry"
        )
        return 1

    try:
        await set_tenant_secret(tenant_id, CLIENT_ID_KEY, registration.client_id)
        await set_tenant_secret(tenant_id, CLIENT_SECRET_KEY, registration.client_secret or "")
        await set_tenant_secret(tenant_id, REFRESH_TOKEN_KEY, refresh_token)
    except TenantSecretError as exc:
        console.print(f"[bold red]could not store the grant in Vault:[/bold red] {exc}")
        return 1

    console.print(
        f"\n[green]✓[/green] {tenant_id} is authorized against Cal.com's MCP server. "
        'Set booking.provider to "mcp_calcom" in its config to start using it '
        '(content/README.md\'s "Booking via MCP" section).'
    )
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--tenant", required=True, help="tenant_id")
    parser.add_argument(
        "--port", type=int, default=None, help="localhost callback port (default: settings)"
    )
    parser.add_argument("--scope", default=None, help="OAuth scope, if the server requires one")
    parser.add_argument(
        "--no-browser", action="store_true", help="print the authorize URL instead of opening it"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    force_utf8_console()
    configure_logging()
    args = _parse_args(argv)
    settings = get_settings()
    port = args.port or settings.calcom_oauth_redirect_port
    return asyncio.run(
        _run(args.tenant, port=port, scope=args.scope, open_browser=not args.no_browser)
    )


if __name__ == "__main__":
    sys.exit(main())
