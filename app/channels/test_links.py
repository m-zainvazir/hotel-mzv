"""Test Agent link signing (Phase 9.1, shared with Phase 9.3's voice tester).

A near-copy of `app/channels/widget_auth.py`'s stdlib HMAC pattern (no PyJWT)
— deliberately a SEPARATE secret and claim set, not a reuse of the widget
session signer, so a leaked test link can never be replayed as a chat
session token, and a leaked chat session token can never be replayed as a
Test Agent link.

`mode` is carried now, even though only `"chat"` is actually usable until
Phase 9.3 ships the voice tester — `"voice"` can be minted today (so an
operator's link doesn't need to change shape later) but every consumer
(`GET /test/{token}`, `POST /test/session`) refuses it until then.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Literal

from app.config import get_settings

#: Same three-tier degradation `app/channels/widget_auth.py` already uses:
#: TEST_LINK_SECRET, else WIDGET_SESSION_SECRET, else a per-process fallback
#: generated once here — so dev needs no new env var, and a leaked test link
#: still can't be replayed as a widget session token even when both fall
#: back to the same secret, because the claim sets differ entirely.
_fallback_secret = secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class TestLinkClaims:
    tenant_id: str
    mode: Literal["chat", "voice"]
    #: "live" is the shareable link that always reflects what's actually
    #: running. "draft" is the Config tab's "Preview draft" button — the
    #: conversation runs against whatever the tenant's current draft is at
    #: the moment each message is sent (re-read fresh every turn, never
    #: baked into the token), falling back to live if the draft was since
    #: deployed or discarded out from under an open preview tab.
    variant: Literal["live", "draft"]


def _secret() -> str:
    settings = get_settings()
    return settings.test_link_secret or settings.widget_session_secret or _fallback_secret


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def mint_test_token(
    tenant_id: str,
    *,
    mode: Literal["chat", "voice"] = "chat",
    variant: Literal["live", "draft"] = "live",
    ttl_seconds: int | None = None,
) -> str:
    now = int(time.time())
    ttl = ttl_seconds if ttl_seconds is not None else get_settings().test_link_ttl_seconds
    payload = {
        "tid": tenant_id,
        "mode": mode,
        "variant": variant,
        "exp": now + ttl,
    }
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256)
    return f"{body}.{_b64url(signature.digest())}"


def verify_test_token(token: str) -> TestLinkClaims | None:
    """Verify a token minted by `mint_test_token`.

    Never raises — malformed, tampered or expired input all just fail closed
    with `None`, matching `verify_session_token`'s posture.
    """
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256)
        if not hmac.compare_digest(_b64url(expected.digest()), signature):
            return None
        payload = json.loads(_b64url_decode(body))
        if int(payload["exp"]) < int(time.time()):
            return None
        mode = str(payload["mode"])
        variant = str(payload["variant"])
        if mode not in ("chat", "voice") or variant not in ("live", "draft"):
            return None
        return TestLinkClaims(tenant_id=str(payload["tid"]), mode=mode, variant=variant)  # type: ignore[arg-type]
    except Exception:
        return None
