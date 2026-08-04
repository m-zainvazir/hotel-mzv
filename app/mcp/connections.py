"""Build `MultiServerMCPClient` connection dicts from tenant config (Phase 6).

Isolated in its own module for the same reason `app/channels/vapi_schema.py`
is: this is the only place that knows another vendor's wire shape, so an
adapter-version change or a new auth quirk is a one-file edit.

Three auth shapes exist now. The first two are for tenant-authored servers in
the wild, and neither can be privileged, because "connect literally any
remote MCP server" has to cover both:

    {"name": "tavily", "transport": "http",
     "url": "https://mcp.tavily.com/mcp/?tavilyApiKey=${secret}",
     "auth_secret_ref": "TAVILY_API_KEY"}

    {"name": "acme_crm", "transport": "http", "url": "https://acme.example/mcp",
     "headers": {"Authorization": "Bearer ${secret}", "X-Acme-Org": "hotel-mzv"},
     "auth_secret_ref": "ACME_CRM_TOKEN"}

`${secret}` is substituted into `url` AND every `headers` value from the
resolved vault secret. When `headers` is empty and a ref is set, the default
is `{"Authorization": "Bearer <secret>"}`.

The third (`auth: "oauth"`, Phase 9 Part A) is Cal.com's hosted MCP server
specifically, never tenant-authored: `McpServerConfig.auth` docstring has the
detail. A bearer is resolved from `app/mcp/oauth.py::access_token_for` at
connect time instead of `${secret}` substitution.

    {"name": "calcom", "transport": "http", "url": "https://mcp.cal.com/mcp",
     "auth": "oauth"}
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.tenancy.models import McpServerConfig
from app.tenancy.secrets import TenantSecretError, resolve_secret

logger = logging.getLogger(__name__)

_PLACEHOLDER = "${secret}"


async def build_connection(tenant_id: str, server: McpServerConfig) -> dict[str, Any] | None:
    """One server's `MultiServerMCPClient` connection dict, or `None` to skip it.

    `None` means: a WARNING was already logged and the caller should move on
    — one misconfigured or disallowed server must never take the others down
    with it.
    """
    settings = get_settings()

    if server.auth == "oauth":
        return await _build_oauth_connection(tenant_id, server, settings)

    if server.transport == "stdio":
        if not settings.mcp_allow_stdio:
            logger.warning(
                "mcp server %s/%s uses transport=stdio but MCP_ALLOW_STDIO is false "
                "— a command string from tenant config is remote code execution on "
                "the one box holding every tenant's data, so this server is skipped. "
                "Set MCP_ALLOW_STDIO=true only for a local server you explicitly trust.",
                tenant_id,
                server.name,
            )
            return None
        return {
            "transport": "stdio",
            "command": server.command,
            "args": list(server.args),
        }

    secret: str | None = None
    if server.auth_secret_ref:
        try:
            secret = await resolve_secret(tenant_id, server.auth_secret_ref, env_value=None)
        except TenantSecretError:
            logger.warning(
                "mcp server %s/%s: could not resolve secret %r — skipping this server",
                tenant_id,
                server.name,
                server.auth_secret_ref,
                exc_info=True,
            )
            return None
        if secret is None:
            logger.warning(
                "mcp server %s/%s: auth_secret_ref %r has no value in Vault — "
                "skipping rather than connecting unauthenticated",
                tenant_id,
                server.name,
                server.auth_secret_ref,
            )
            return None

    url = server.url or ""
    headers = dict(server.headers)
    if secret is not None:
        url = url.replace(_PLACEHOLDER, secret)
        if headers:
            headers = {key: value.replace(_PLACEHOLDER, secret) for key, value in headers.items()}
        else:
            headers = {"Authorization": f"Bearer {secret}"}

    return {
        "transport": "streamable_http",
        "url": url,
        "headers": headers,
        "timeout": float(settings.mcp_connect_timeout_seconds),
        "sse_read_timeout": float(settings.mcp_tool_timeout_seconds),
    }


async def _build_oauth_connection(
    tenant_id: str, server: McpServerConfig, settings: Any
) -> dict[str, Any] | None:
    """`auth: "oauth"` — currently Cal.com's hosted MCP server only.

    Mirrors the return contract every other branch of `build_connection`
    already has (`None` + a logged WARNING on failure, never a raised
    exception) so the generic tenant long-tail path
    (`app/mcp/client.py::_build_connections`) keeps degrading the same way it
    always has. `app/tools/booking/mcp_calcom.py` — where a resolution
    failure must become a spoken `BookingError`, not a silently dropped
    server — turns a `None` back into that error itself rather than this
    function raising, so both callers get the behaviour they each need from
    one shared implementation.
    """
    from app.mcp.oauth import CalcomOAuthError, access_token_for

    if not server.url:
        logger.warning(
            "mcp server %s/%s has auth='oauth' but no url — skipping", tenant_id, server.name
        )
        return None

    try:
        token = await access_token_for(tenant_id)
    except CalcomOAuthError:
        logger.warning(
            "mcp server %s/%s: could not resolve an OAuth access token — skipping this "
            "server this turn",
            tenant_id,
            server.name,
            exc_info=True,
        )
        return None

    return {
        "transport": "streamable_http",
        "url": server.url,
        "headers": {"Authorization": f"Bearer {token}"},
        "timeout": float(settings.mcp_connect_timeout_seconds),
        "sse_read_timeout": float(settings.mcp_tool_timeout_seconds),
    }


def redacted(connection: dict[str, Any]) -> dict[str, Any]:
    """A connection dict safe to log — no raw MCP URL or header value.

    With query-parameter auth (Tavily's `?tavilyApiKey=...`) the URL *is* the
    credential, so this strips more aggressively than
    `app/channels/vapi_schema.redacted()`: not just known-sensitive keys, the
    entire query string and every header value, unconditionally.
    """
    out = dict(connection)
    if "url" in out and isinstance(out["url"], str):
        out["url"] = _redact_url(out["url"])
    if "headers" in out and isinstance(out["headers"], dict):
        out["headers"] = dict.fromkeys(out["headers"], "***")
    return out


def _redact_url(url: str) -> str:
    if "?" in url:
        base, _, _ = url.partition("?")
        return f"{base}?***"
    return url
