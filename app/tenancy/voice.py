"""Cartesia voice cloning + consent enforcement (Phase 4 Step 8).

CLAUDE.md convention #6 has no exceptions: a tenant's voice may only be
cloned, and its `voice_id` only ever set, once a signed consent record
exists. Enforced twice — here in Python (`clone_voice` is the only path that
should ever call Cartesia's clone endpoint) and as a DB trigger in
`app/db/migrations/0005_voice_consent.sql` on the `tenants` table itself —
because "no exceptions" has to survive someone running SQL by hand.

This is an onboarding-time action, not a per-conversation tool: it goes
through the Supabase **secret** key (admin path, bypasses RLS), never a
tenant-scoped JWT — recording another tenant's consent, or checking whether
one exists, isn't a "this tenant's own data" operation the way booking a job
is.
"""

from __future__ import annotations

import os

import httpx

from app.config import get_settings

_CLONE_PATH = "/voices/clone"


class VoiceConsentError(RuntimeError):
    """No recorded consent — refuse to clone or to claim one exists."""


class CartesiaError(RuntimeError):
    """Cartesia's API rejected the request."""


def _admin_client(settings, *, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{settings.supabase_url}/rest/v1",
        headers={
            "apikey": settings.supabase_secret_key or "",
            "Authorization": f"Bearer {settings.supabase_secret_key}",
        },
        timeout=timeout,
    )


async def record_consent(
    *,
    tenant_id: str,
    voice_owner_name: str,
    consent_artifact_url: str,
    granted_by: str,
    consent_artifact_hash: str | None = None,
    sample_audio_hash: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Write the `voice_consents` row. Must happen before `clone_voice` will
    do anything — this is the "written consent, stored" half of convention #6."""
    settings = get_settings()
    if not settings.supabase_url:
        raise VoiceConsentError("SUPABASE_URL is not configured — cannot record consent")

    owns_client = client is None
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
    try:
        response = await active.post(
            "/voice_consents",
            headers={"Prefer": "return=representation"},
            json={
                "tenant_id": tenant_id,
                "voice_owner_name": voice_owner_name,
                "consent_artifact_url": consent_artifact_url,
                "consent_artifact_hash": consent_artifact_hash,
                "sample_audio_hash": sample_audio_hash,
                "granted_by": granted_by,
            },
        )
    finally:
        if owns_client:
            await active.aclose()

    if response.status_code >= 400:
        raise VoiceConsentError(
            f"could not record consent: {response.status_code} {response.text[:300]}"
        )
    rows = response.json()
    return rows[0] if isinstance(rows, list) else rows


async def has_consent(tenant_id: str, *, client: httpx.AsyncClient | None = None) -> bool:
    settings = get_settings()
    if not settings.supabase_url:
        return False

    owns_client = client is None
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
    try:
        response = await active.get(
            "/voice_consents", params={"tenant_id": f"eq.{tenant_id}", "limit": "1"}
        )
    finally:
        if owns_client:
            await active.aclose()

    if response.status_code >= 400:
        return False
    return bool(response.json())


async def clone_voice(
    *,
    tenant_id: str,
    audio_path: str,
    voice_name: str,
    language: str = "en",
    tagline: str = "",
    consent_client: httpx.AsyncClient | None = None,
    cartesia_client: httpx.AsyncClient | None = None,
) -> str:
    """Clone a voice from a local audio file and return the new `voice_id`.

    Refuses — without ever calling Cartesia — if no `voice_consents` row
    exists for this tenant.
    """
    if not await has_consent(tenant_id, client=consent_client):
        raise VoiceConsentError(
            f"no voice_consents row for tenant {tenant_id!r} — call record_consent() first"
        )

    settings = get_settings()
    if not settings.cartesia_api_key:
        raise CartesiaError("Cartesia is not configured (CARTESIA_API_KEY unset)")

    owns_client = cartesia_client is None
    # Its own client, not the shared one other providers use — a multipart
    # audio upload needs a much longer timeout than a booking-shaped call
    # (see cartesia_clone_timeout_seconds).
    active = cartesia_client or httpx.AsyncClient(
        base_url=settings.cartesia_api_base,
        headers={
            "Authorization": f"Bearer {settings.cartesia_api_key}",
            "Cartesia-Version": settings.cartesia_api_version,
        },
        timeout=settings.cartesia_clone_timeout_seconds,
    )
    try:
        with open(audio_path, "rb") as clip:
            response = await active.post(
                _CLONE_PATH,
                files={"clip": (os.path.basename(audio_path), clip)},
                data={"name": voice_name, "language": language, "tagline": tagline[:32]},
            )
    finally:
        if owns_client:
            await active.aclose()

    if response.status_code >= 400:
        raise CartesiaError(f"Cartesia clone failed: {response.status_code} {response.text[:300]}")
    data = response.json()
    voice_id = data.get("id")
    if not voice_id:
        raise CartesiaError(f"Cartesia response had no voice id: {data}")
    return voice_id
