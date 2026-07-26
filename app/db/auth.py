"""Mints short-lived per-tenant JWTs so Supabase RLS policies can enforce
`auth.jwt() ->> 'tenant_id'` (Phase 4 Step 5).

Sign-only — nothing here ever verifies a token, PostgREST does that. ~20
lines of stdlib `hmac`/`hashlib`/`base64` rather than a PyJWT dependency,
matching the Phase 3 "raw httpx, no SDKs" precedent (plan §10 amendment).

`role: "app_backend"`, not `"authenticated"` — `authenticated` is GoTrue's
role for end users, and the moment a customer-facing dashboard exists on
Supabase Auth, those users would inherit every grant written for this
backend (see `app/db/migrations/0002_rls.sql`). `app_backend` is `nologin`
and reachable only by a JWT signed with this project's own secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from threading import RLock

from app.config import get_settings

#: Short enough that a leaked token is useless quickly; long enough that a
#: single conversational turn never straddles an expiry.
_EXPIRY_SECONDS = 300
#: Not for CPU — HMAC-SHA256 over ~150 bytes is microseconds — but to have
#: one place to invalidate, the same rationale as the shared-client cache in
#: app/tools/http_client.py.
_CACHE_TTL_SECONDS = 60

_lock = RLock()
_cache: dict[str, tuple[float, str]] = {}


class SupabaseAuthNotConfiguredError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _mint(tenant_id: str, secret: str, *, now: int | None = None) -> str:
    issued_at = now if now is not None else int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": "ai-receptionist-backend",
        # Supabase's PostgREST is configured with a jwt-aud check — a
        # missing/wrong audience is a silent 401 that looks like an RLS
        # denial. "authenticated" matches what GoTrue itself issues.
        "aud": "authenticated",
        "role": "app_backend",
        "tenant_id": tenant_id,
        "sub": tenant_id,
        "iat": issued_at,
        "exp": issued_at + _EXPIRY_SECONDS,
    }
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256)
    return f"{signing_input}.{_b64url(signature.digest())}"


def tenant_jwt(tenant_id: str) -> str:
    """A short-lived JWT scoped to `tenant_id`, cached briefly."""
    settings = get_settings()
    if not settings.supabase_jwt_secret:
        raise SupabaseAuthNotConfiguredError(
            "SUPABASE_JWT_SECRET is unset — cannot mint a tenant-scoped RLS token "
            "(Settings -> API -> JWT Settings -> Legacy JWT secret)."
        )

    now = time.monotonic()
    with _lock:
        cached = _cache.get(tenant_id)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]

    token = _mint(tenant_id, settings.supabase_jwt_secret)
    with _lock:
        _cache[tenant_id] = (now, token)
    return token


def clear_jwt_cache() -> None:
    """Test hook — drop cached tokens so a swapped secret takes effect."""
    with _lock:
        _cache.clear()
