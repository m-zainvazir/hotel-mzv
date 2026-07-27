"""Shared channel auth.

Every public endpoint runs the brain, which costs money and can send SMS — so
each is gated by a shared secret whenever one is configured. Development with
no secret set stays open for convenience.

Four schemes, because four kinds of caller:
  * `require_chat_caller` — `POST /chat` accepts either a widget session token
    (minted by `POST /chat/session`, public) or the shared `API_AUTH_TOKEN`
    bearer (server-to-server: `chat_cli`, tests, another backend). Which one
    was presented decides whether tenancy comes from the verified token or
    from the request body — see `app/channels/chat.py`.
  * `require_vapi_secret` — Vapi's own scheme, which is either a plain shared
    secret header or an HMAC over the raw body depending on how the assistant's
    `server` block is set up. We accept both rather than guess.
  * `is_ops_caller` — `GET /health`'s detail fields (Phase 7 Step 4). Unlike
    the other three, it never raises: a health check must always get a 200,
    just with less detail without the right bearer.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Literal

from fastapi import Header, HTTPException, Request, status

from app.channels.vapi_schema import SECRET_HEADER, SIGNATURE_HEADER
from app.channels.widget_auth import SessionClaims, verify_session_token
from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ChatAuth:
    #: "widget" — a verified session token; tenant/session come from `claims`,
    #: never the request body. "trusted" — the shared bearer (or no secret is
    #: configured at all, the dev default); tenant/session come from the body.
    mode: Literal["widget", "trusted"]
    claims: SessionClaims | None = None


async def require_chat_caller(authorization: str | None = Header(default=None)) -> ChatAuth:
    """Resolve which of the two accepted `/chat` callers this is.

    A widget session token is tried first — it is self-verifying (HMAC), so
    attempting it on an arbitrary bearer value is safe and simply fails
    closed. Falling through to the shared-secret comparison exactly
    reproduces the pre-Phase-5 behaviour for every other caller.
    """
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()

    claims = verify_session_token(presented) if presented else None
    if claims is not None:
        return ChatAuth(mode="widget", claims=claims)

    expected = get_settings().api_auth_token
    if not expected:
        return ChatAuth(mode="trusted")
    if presented and hmac.compare_digest(presented, expected):
        return ChatAuth(mode="trusted")

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing bearer token",
    )


def is_ops_caller(authorization: str | None = Header(default=None)) -> bool:
    """Whether `GET /health` should include its authenticated detail fields
    (`env`, `llm_provider`, `model`, the full `tenants` list — Phase 7 Step 4
    moved these behind auth; a `/health` scraped from outside the org
    shouldn't hand over the tenant roster and stack details for free).

    Same `API_AUTH_TOKEN` bearer as `require_chat_caller`'s trusted path, but
    this never raises — a health checker with no `Authorization` header
    still needs a 200, just a less detailed one. No token configured (the
    dev default) means every caller sees the detail, matching this module's
    fail-open-when-unconfigured convention.
    """
    expected = get_settings().api_auth_token
    if not expected:
        return True
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    presented = authorization[7:].strip()
    return hmac.compare_digest(presented, expected)


async def require_vapi_secret(
    request: Request,
    x_vapi_secret: str | None = Header(default=None, alias=SECRET_HEADER),
    x_vapi_signature: str | None = Header(default=None, alias=SIGNATURE_HEADER),
) -> None:
    """Authenticate a request from Vapi.

    Vapi sends `server.secret` either verbatim in `x-vapi-secret` or as an
    HMAC-SHA256 of the raw body in `x-vapi-signature`, depending on the
    assistant's configuration. Accepting both means a config change on their
    side can't silently lock us out mid-call.
    """
    expected = get_settings().vapi_webhook_secret
    if not expected:
        return

    if x_vapi_signature:
        body = await request.body()
        digest = hmac.new(expected.encode(), body, hashlib.sha256).hexdigest()
        # Some senders prefix the scheme; compare against the bare hex too.
        candidate = x_vapi_signature.split("=")[-1].strip()
        if hmac.compare_digest(candidate, digest):
            return
        logger.warning("vapi signature mismatch on %s", request.url.path)

    if x_vapi_secret and hmac.compare_digest(x_vapi_secret, expected):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing Vapi credentials",
    )
