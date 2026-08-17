"""Cal.com's hosted MCP server OAuth 2.1 flow (Phase 9 Part A).

Split the same way `app/db/auth.py` is: an interactive half that runs once
per tenant (`scripts/authorize_calcom.py`, using `discover` / `register_client`
/ `build_authorize_url` / `exchange_code` below) and a headless half that runs
forever (`access_token_for`, called from `app/tools/booking/mcp_calcom.py` on
every turn).

**Step A0 spike findings (2026-08-01, recorded here since Cal.com's docs name
the MCP tools but not the OAuth wiring around them):**

    GET https://mcp.cal.com/.well-known/oauth-protected-resource
      -> {"resource": "https://mcp.cal.com",
          "authorization_servers": ["https://mcp.cal.com"],
          "bearer_methods_supported": ["header"]}

    GET https://mcp.cal.com/.well-known/oauth-authorization-server
      -> {"issuer": "https://mcp.cal.com",
          "authorization_endpoint": "https://mcp.cal.com/oauth/authorize",
          "token_endpoint": "https://mcp.cal.com/oauth/token",
          "registration_endpoint": "https://mcp.cal.com/oauth/register",
          "revocation_endpoint": "https://mcp.cal.com/oauth/revoke",
          "grant_types_supported": ["authorization_code", "refresh_token"],
          "code_challenge_methods_supported": ["S256"],
          "token_endpoint_auth_methods_supported": ["none"]}

    POST https://mcp.cal.com/oauth/register {client_name, redirect_uris,
        grant_types, response_types, token_endpoint_auth_method: "none"}
      -> 201 {"client_id": "<uuid>", "redirect_uris": [...], "client_name": ...}
      (no client_secret — a public client, PKCE-only, matching
      token_endpoint_auth_methods_supported above)

Both gates the plan's Step A0 names — DCR and refresh tokens — are open, so
Part A uses the hosted server; the first-party `scripts/calcom_mcp_server.py`
fallback the plan describes is not needed.

**Update (2026-08-01, plan §9 live check 3, run end to end against a real
grant):** `get_availability` / `create_booking`'s argument shapes are
confirmed, not just guessed — a real availability query and a real booking
both succeeded through the hosted server against `hotel-mzv`'s own Cal.com
account, and the resulting booking matched an equivalent `"calcom"`-provider
booking (same event type, duration, attendee-email pattern, metadata shape)
when pulled back from Cal.com's own `/v2/bookings` API. See
`app/tools/booking/mcp_calcom.py`'s module docstring for the detail —
`cancel_booking`/`reschedule_booking` remain unverified since nothing calls
them yet.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from app.config import get_settings
from app.tenancy.secrets import (
    TenantSecretError,
    invalidate_tenant_secret_cache,
    resolve_secret,
    set_tenant_secret,
)

logger = logging.getLogger(__name__)

REFRESH_TOKEN_KEY = "calcom_mcp_refresh_token"
CLIENT_ID_KEY = "calcom_mcp_client_id"
CLIENT_SECRET_KEY = "calcom_mcp_client_secret"

_DEFAULT_TIMEOUT_SECONDS = 10.0


class CalcomOAuthError(RuntimeError):
    """Discovery, registration, a token exchange, or a refresh failed.

    Callers on the booking critical path must map this to `BookingError`,
    never let it escape raw — the same rule
    `app/tools/booking/calcom.py` already follows for every Cal.com failure.
    """


@dataclass(frozen=True)
class OAuthMetadata:
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None


@dataclass(frozen=True)
class ClientRegistration:
    client_id: str
    client_secret: str | None


@dataclass(frozen=True)
class PkcePair:
    verifier: str
    challenge: str


def generate_pkce_pair() -> PkcePair:
    """RFC 7636 S256 — the only method Cal.com's discovery advertises."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return PkcePair(verifier=verifier, challenge=challenge)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


async def discover(resource_url: str, *, client: httpx.AsyncClient | None = None) -> OAuthMetadata:
    """Protected-resource metadata -> authorization-server metadata (RFC 9728
    then RFC 8414). Doesn't hardcode Cal.com naming itself as its own
    authorization server, even though that's what live discovery returns
    today — a future split into a separate auth host shouldn't need a code
    change here.
    """
    owns_client = client is None
    active = client or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS)
    origin = _origin(resource_url)
    try:
        resource_meta = await _get_json(active, f"{origin}/.well-known/oauth-protected-resource")
        auth_servers = resource_meta.get("authorization_servers") or [origin]
        auth_server = auth_servers[0]
        server_meta = await _get_json(
            active, f"{auth_server}/.well-known/oauth-authorization-server"
        )
    except (httpx.HTTPError, ValueError) as exc:
        raise CalcomOAuthError(f"OAuth discovery failed for {resource_url}: {exc}") from exc
    finally:
        if owns_client:
            await active.aclose()

    try:
        return OAuthMetadata(
            authorization_endpoint=server_meta["authorization_endpoint"],
            token_endpoint=server_meta["token_endpoint"],
            registration_endpoint=server_meta.get("registration_endpoint"),
        )
    except KeyError as exc:
        raise CalcomOAuthError(f"OAuth metadata missing {exc} at {auth_server}") from exc


async def _get_json(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    response = await client.get(url)
    response.raise_for_status()
    return response.json()


async def register_client(
    metadata: OAuthMetadata,
    *,
    redirect_uri: str,
    client_name: str = "ai-receptionist",
    client: httpx.AsyncClient | None = None,
) -> ClientRegistration:
    """RFC 7591 Dynamic Client Registration — one call, no pre-shared secret."""
    if not metadata.registration_endpoint:
        raise CalcomOAuthError("this authorization server does not support DCR")

    owns_client = client is None
    active = client or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS)
    try:
        response = await active.post(
            metadata.registration_endpoint,
            json={
                "client_name": client_name,
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
    except httpx.HTTPError as exc:
        raise CalcomOAuthError(f"client registration failed: {exc}") from exc
    finally:
        if owns_client:
            await active.aclose()

    if response.status_code >= 400:
        raise CalcomOAuthError(
            f"client registration rejected ({response.status_code}): {response.text[:200]}"
        )

    data = response.json()
    try:
        client_id = data["client_id"]
    except KeyError as exc:
        raise CalcomOAuthError("registration response missing client_id") from exc
    return ClientRegistration(client_id=client_id, client_secret=data.get("client_secret"))


def build_authorize_url(
    metadata: OAuthMetadata,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scope: str | None = None,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if scope:
        params["scope"] = scope
    return f"{metadata.authorization_endpoint}?{urlencode(params)}"


async def exchange_code(
    metadata: OAuthMetadata,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str | None,
    code_verifier: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """The one-time authorization-code -> token exchange. Returns the raw
    token response (`access_token`, `refresh_token`, `expires_in`, ...) so
    the caller (`scripts/authorize_calcom.py`) decides what to persist."""
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        body["client_secret"] = client_secret
    return await _post_token(metadata.token_endpoint, body, client=client)


async def _post_token(
    token_endpoint: str, body: dict[str, str], *, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    owns_client = client is None
    active = client or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS)
    try:
        response = await active.post(token_endpoint, data=body)
    except httpx.TimeoutException as exc:
        raise CalcomOAuthError("the token endpoint did not respond in time") from exc
    except httpx.HTTPError as exc:
        raise CalcomOAuthError(f"could not reach the token endpoint: {exc}") from exc
    finally:
        if owns_client:
            await active.aclose()

    if response.status_code >= 400:
        # Logged, never re-raised verbatim — this module sits under the same
        # "raw provider text never leaks upstream" rule as calcom.py.
        logger.warning(
            "calcom oauth token endpoint %d: %s", response.status_code, response.text[:200]
        )
        raise CalcomOAuthError(
            f"token request rejected ({response.status_code}) — the grant may be revoked"
        )

    return response.json()


# --- headless refresh (the forever-running half) ----------------------------

_lock = RLock()
#: tenant_id -> (expires_at_epoch_seconds, access_token)
_cache: dict[str, tuple[float, str]] = {}
#: resource origin -> (cached_at_monotonic, OAuthMetadata). Discovery
#: metadata changes essentially never; this just saves two round trips on
#: every single refresh, not a correctness requirement like the MCP tool
#: cache in app/mcp/client.py.
_metadata_cache: dict[str, tuple[float, OAuthMetadata]] = {}
_METADATA_CACHE_TTL_SECONDS = 3600.0


async def access_token_for(tenant_id: str, *, client: httpx.AsyncClient | None = None) -> str:
    """A valid Cal.com MCP access token for `tenant_id`, refreshing when the
    cached one is stale.

    Raises `CalcomOAuthError` — never a bare exception — when the tenant has
    no grant at all or the grant has been revoked; the booking-tool boundary
    (`app/tools/booking/mcp_calcom.py`) maps that to the same recoverable
    "calendar is not responding right now" string every other Cal.com
    failure produces.
    """
    settings = get_settings()
    now = time.time()

    with _lock:
        cached = _cache.get(tenant_id)
    if cached and now < cached[0]:
        return cached[1]

    refresh_token, client_id, client_secret = await _load_credentials(tenant_id)
    metadata = await _cached_metadata(settings.calcom_mcp_url, client=client)

    def _body(token: str, cid: str, secret: str | None) -> dict[str, str]:
        body = {"grant_type": "refresh_token", "refresh_token": token, "client_id": cid}
        if secret:
            body["client_secret"] = secret
        return body

    try:
        data = await _post_token(
            metadata.token_endpoint, _body(refresh_token, client_id, client_secret), client=client
        )
    except CalcomOAuthError:
        # Phase 9.4: Cal.com rotates the refresh token on EVERY refresh and
        # invalidates the previous one, so a grant shared by more than one
        # process — a dev box and Railway both serving the same tenant, or two
        # replicas — is a rotation race by construction. Whichever process
        # refreshes second presents a value the other already spent.
        #
        # The loser's fix is simply to re-read: the winner persisted the new
        # token to Vault before handing its own out. Drop the cached copy,
        # read again, and try once more. A genuinely revoked grant fails the
        # retry too and raises normally, so this can't mask a dead grant — and
        # it can't loop, since the retry never retries itself.
        invalidate_tenant_secret_cache(tenant_id)
        retry_token, retry_client_id, retry_secret = await _load_credentials(tenant_id)
        if retry_token == refresh_token:
            raise  # nothing changed underneath us — the grant really is bad
        logger.info(
            "calcom oauth refresh for %s lost a rotation race — retrying with the "
            "token another process just persisted",
            tenant_id,
        )
        data = await _post_token(
            metadata.token_endpoint,
            _body(retry_token, retry_client_id, retry_secret),
            client=client,
        )
        refresh_token = retry_token

    try:
        access_token = data["access_token"]
    except KeyError as exc:
        raise CalcomOAuthError("token refresh response missing access_token") from exc

    expires_in = data.get("expires_in")
    if isinstance(expires_in, int | float) and expires_in > 0:
        # Refresh a safety margin before the real expiry so a call never
        # straddles it mid-request.
        margin = min(30.0, expires_in * 0.25)
        ttl = max(expires_in - margin, 5.0)
    else:
        ttl = float(settings.calcom_oauth_token_cache_seconds)
    expires_at = now + ttl

    with _lock:
        _cache[tenant_id] = (expires_at, access_token)

    # Some authorization servers rotate the refresh token on every use —
    # if this one did, the old value in Vault would fail on the NEXT
    # refresh, so persist it immediately rather than silently dropping it.
    new_refresh_token = data.get("refresh_token")
    if new_refresh_token and new_refresh_token != refresh_token:
        try:
            await set_tenant_secret(tenant_id, REFRESH_TOKEN_KEY, new_refresh_token)
        except TenantSecretError:
            logger.warning(
                "calcom oauth issued a rotated refresh token for %s but it could not be "
                "persisted to Vault — the NEXT refresh will fail with a stale token",
                tenant_id,
                exc_info=True,
            )

    return access_token


async def _load_credentials(tenant_id: str) -> tuple[str, str, str | None]:
    try:
        refresh_token = await resolve_secret(tenant_id, REFRESH_TOKEN_KEY, env_value=None)
        client_id = await resolve_secret(tenant_id, CLIENT_ID_KEY, env_value=None)
        client_secret = await resolve_secret(tenant_id, CLIENT_SECRET_KEY, env_value=None)
    except TenantSecretError as exc:
        raise CalcomOAuthError(
            f"could not resolve Cal.com OAuth credentials for {tenant_id}"
        ) from exc

    if not refresh_token or not client_id:
        raise CalcomOAuthError(
            f"{tenant_id} has no Cal.com MCP authorization — run "
            f"`python -m scripts.authorize_calcom --tenant {tenant_id}`"
        )
    return refresh_token, client_id, client_secret or None


async def has_grant(tenant_id: str) -> bool:
    """Whether this tenant has completed the Cal.com OAuth flow (Phase 9.4).

    Deliberately NOT `access_token_for` with the error caught: that performs a
    real refresh against Cal.com's token endpoint, and this is called to draw
    a badge in the admin panel. Rendering a page must not spend a network
    round trip — or worse, burn a refresh token — per tenant.

    A vault *error* answers False here, unlike `resolve_secret`'s "an error is
    never absent" rule (CLAUDE.md). That rule exists to stop a failed lookup
    silently falling back to a shared credential and booking into the wrong
    account; nothing falls back here, and the only consequence of a wrong
    False is the panel showing "not connected" while the bot keeps booking
    perfectly well.
    """
    try:
        await _load_credentials(tenant_id)
    except CalcomOAuthError:
        return False
    return True


async def _cached_metadata(resource_url: str, *, client: httpx.AsyncClient | None) -> OAuthMetadata:
    origin = _origin(resource_url)
    now = time.monotonic()
    with _lock:
        cached = _metadata_cache.get(origin)
    if cached and now - cached[0] < _METADATA_CACHE_TTL_SECONDS:
        return cached[1]

    metadata = await discover(resource_url, client=client)
    with _lock:
        _metadata_cache[origin] = (now, metadata)
    return metadata


def invalidate(tenant_id: str) -> None:
    """Drop `tenant_id`'s cached access token.

    Call this when a live MCP tool call using the cached token fails with
    what looks like an auth rejection, so the *next* attempt forces a fresh
    refresh instead of retrying with a token the server has already
    rejected once this process.
    """
    with _lock:
        _cache.pop(tenant_id, None)


def clear_calcom_oauth_cache() -> None:
    """Test hook — drop cached access tokens and discovery metadata."""
    with _lock:
        _cache.clear()
        _metadata_cache.clear()
