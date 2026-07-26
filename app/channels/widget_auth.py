"""Widget session tokens (Phase 5).

Signs **and** verifies — unlike `app/db/auth.py`, which only signs (PostgREST
does verification there). This is the one place in the codebase with token
verification logic, so it stays small and gets tested hard: it's what stands
between "any browser" and "any tenant's chat".

Stdlib `hmac`/`hashlib`/`base64` only, mirroring `app/db/auth.py`'s shape —
that file already proved the pattern here and is why PyJWT isn't a dependency.

A minted token binds one `session_id` to one `tenant_id`. `POST /chat`
(`app/channels/chat.py`) trusts a verified token for tenancy on the public
path and never the request body — the same invariant `app/channels/vapi_llm.py`
already states for voice, finally enforced on chat too.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from app.config import get_settings

#: Fallback signing key when WIDGET_SESSION_SECRET is unset — generated once
#: per process so development works with zero config. Tokens simply don't
#: survive a restart, matching the fail-open-when-unconfigured convention in
#: app/channels/security.py. Never used once a real secret is configured.
_fallback_secret = secrets.token_urlsafe(32)


@dataclass(slots=True, frozen=True)
class SessionClaims:
    tenant_id: str
    session_id: str


def _secret() -> str:
    return get_settings().widget_session_secret or _fallback_secret


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def new_session_id() -> str:
    return f"web_{secrets.token_hex(6)}"


def mint_session_token(tenant_id: str, session_id: str) -> str:
    """A short-lived token binding one widget session to one tenant."""
    now = int(time.time())
    payload = {
        "tid": tenant_id,
        "sid": session_id,
        "exp": now + get_settings().widget_session_ttl_seconds,
    }
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256)
    return f"{body}.{_b64url(signature.digest())}"


def verify_session_token(token: str) -> SessionClaims | None:
    """Verify a token minted by `mint_session_token`.

    Never raises — malformed, tampered or expired input all just fail closed
    with `None`, the same "degrade, don't 500" posture as the rest of the
    channel layer (see `app/channels/vapi_schema.py`'s lenient parsing).
    """
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256)
        if not hmac.compare_digest(_b64url(expected.digest()), signature):
            return None
        payload = json.loads(_b64url_decode(body))
        if int(payload["exp"]) < int(time.time()):
            return None
        return SessionClaims(tenant_id=str(payload["tid"]), session_id=str(payload["sid"]))
    except Exception:
        return None
