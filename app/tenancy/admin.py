"""Admin write path (Phase 8) — the third caller of `sync_tenant()`.

Kept in its own module, separate from `app/channels/admin.py`'s read routes:
this operates through the Supabase **secret** key (an operator/backend
action — the same reasoning `app/tenancy/sync.py`'s own docstring gives for
the write side), while every *read* in the admin API goes through the
tenant-scoped JWT `AnalyticsStore` already mints. Mixing the two credentials
in one file is exactly the kind of thing that gets copy-pasted wrong later,
and it's also the seam plans/phase8.md's "tenant login later" contract
depends on: when a tenant-scoped write variant exists, only this module
needs to change.

The `TenantRepository` protocol stays read-only (`app/tenancy/repository.py`)
— adding `save()` there would force `JsonFileTenantRepository` and the test
suite's `_OverrideRepository` to implement a write neither has business
implementing. Writes go through `sync_tenant()` instead, extended here with
the checks Pydantic can't perform on its own: optimistic concurrency (does a
row already exist with a different `updated_at`?) and the voice-consent gate
(has this tenant's `voice_id` change actually been consented to?).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.tenancy.loader import clear_tenant_cache, get_repository
from app.tenancy.models import TenantConfig
from app.tenancy.sync import TenantSyncError, sync_tenant

logger = logging.getLogger(__name__)

#: Fields no principal other than an operator may change, even with perfect
#: auth. Shipped now, inert — every principal today IS an operator, so this
#: costs ten minutes; auditing forty fields later, under time pressure, once
#: real tenant logins exist, costs a day and risks missing one.
#:   * tenant_id/status/phone_numbers/widget_keys/vapi — identity and
#:     telephony wiring a tenant has no business repointing.
#:   * booking.event_type_id — redirects every future booking to a
#:     different Cal.com calendar.
#:   * voice.voice_id — bypassing this bypasses the voice-consent gate for a
#:     tenant login that has no business granting consent on someone else's
#:     behalf.
#:   * mcp_servers — a tenant-submitted server URL is an SSRF vector
#:     (plans/phase10.md item 12) the moment it isn't operator-typed.
OPERATOR_ONLY_PATHS: frozenset[str] = frozenset(
    {
        "tenant_id",
        "status",
        "phone_numbers",
        "widget_keys",
        "vapi",
        "booking.event_type_id",
        "voice.voice_id",
        "mcp_servers",
    }
)


class VersionConflictError(RuntimeError):
    """`expected_version` doesn't match the row's current `updated_at` —
    someone else (or another browser tab) saved first."""


class VoiceConsentRequiredError(RuntimeError):
    """Raised by both the pre-check (before any write reaches Postgres) and
    by mapping a PostgREST error body naming `voice_consents` — covers the
    race between the two, with the same actionable message either way."""


def _get_path(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def operator_only_violations(current: TenantConfig, proposed: TenantConfig) -> list[str]:
    """Which `OPERATOR_ONLY_PATHS` actually changed between `current` and
    `proposed`. A non-operator principal changing none of these is free to
    save; changing any of them is 403'd by the caller (`app/channels/admin.py`)."""
    current_dump = current.model_dump(mode="json")
    proposed_dump = proposed.model_dump(mode="json")
    return [
        path
        for path in sorted(OPERATOR_ONLY_PATHS)
        if _get_path(current_dump, path) != _get_path(proposed_dump, path)
    ]


def _admin_client(settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{settings.supabase_url}/rest/v1",
        headers={
            "apikey": settings.supabase_secret_key or "",
            "Authorization": f"Bearer {settings.supabase_secret_key}",
        },
        timeout=settings.supabase_timeout_seconds,
    )


async def _current_row(
    tenant_id: str, client: httpx.AsyncClient
) -> dict[str, Any] | None:
    response = await client.get(
        "/tenants",
        params={"tenant_id": f"eq.{tenant_id}", "select": "updated_at,config", "limit": "1"},
    )
    if response.status_code >= 400 or not response.content:
        return None
    rows = response.json()
    return rows[0] if rows else None


async def _voice_consent_exists(tenant_id: str, client: httpx.AsyncClient) -> bool:
    response = await client.get(
        "/voice_consents", params={"tenant_id": f"eq.{tenant_id}", "limit": "1"}
    )
    if response.status_code >= 400:
        # Fail toward requiring consent, not toward silently allowing a
        # clone through on a transient Postgres hiccup — the safe direction
        # for a check this cheap to simply retry.
        return False
    return bool(response.json()) if response.content else False


async def get_tenant_version(
    tenant_id: str, *, client: httpx.AsyncClient | None = None
) -> str | None:
    """The `tenants.updated_at` value for optimistic concurrency — `None`
    when Supabase isn't configured (dev/test's `TENANT_SOURCE=json`), which
    `save_tenant` reads as "skip the version check entirely"."""
    settings = get_settings()
    owns_client = client is None
    # The credential check only applies when we'd have to build our own
    # client — an injected `client=` (tests) is used as-is regardless of
    # what Settings currently holds, the same fix Step 1's
    # SupabaseTenantRepository needed for the identical reason.
    if owns_client and (not settings.supabase_url or not settings.supabase_secret_key):
        return None
    active = client or _admin_client(settings)
    try:
        row = await _current_row(tenant_id, active)
        return row.get("updated_at") if row else None
    finally:
        if owns_client:
            await active.aclose()


async def save_tenant(
    config: TenantConfig,
    *,
    expected_version: str | None,
    client: httpx.AsyncClient | None = None,
) -> TenantConfig:
    """Validate (already done by the caller via `TenantConfig.model_validate`
    — Pydantic's own validators are the entire validation layer), pre-check
    voice consent, upsert + delete-of-absent-children via `sync_tenant`, then
    invalidate every cache so the very next turn sees the change.

    Order matters: the version check and the voice-consent pre-check both
    read the row that exists *before* this write, so they run before
    `sync_tenant` ever touches Postgres — a rejected write must leave
    nothing behind to roll back.
    """
    settings = get_settings()
    owns_client = client is None
    active = client or _admin_client(settings)
    try:
        current = await _current_row(config.tenant_id, active)

        if expected_version is not None:
            stored_version = current.get("updated_at") if current else None
            if stored_version != expected_version:
                raise VersionConflictError(
                    f"tenant {config.tenant_id!r} was changed by someone else since "
                    "you loaded it — reload and re-apply your edit"
                )

        current_voice_id = (
            ((current or {}).get("config") or {}).get("voice", {}).get("voice_id")
            if current
            else None
        )
        if config.voice.voice_id and config.voice.voice_id != current_voice_id:
            if not await _voice_consent_exists(config.tenant_id, active):
                raise VoiceConsentRequiredError(
                    f"tenant {config.tenant_id!r} has no recorded voice consent for "
                    f"voice_id={config.voice.voice_id!r} — run `python -m "
                    "scripts.onboard_tenant --config <file> --voice-sample <wav> "
                    "--consent-url <url> --consent-owner <name> --consent-granted-by "
                    "<name>` first (CLAUDE.md convention #6 has no exceptions)"
                )

        try:
            await sync_tenant(config, client=active)
        except TenantSyncError as exc:
            # The race this pre-check doesn't close: a consent row deleted
            # between the check above and this write. ⚠️ VERIFY whether
            # PostgREST maps an unqualified `raise exception` to 400 or 500
            # on this project's version — the pre-check exists partly so the
            # good message above doesn't depend on getting this mapping right.
            if "voice_consents" in str(exc) or "P0001" in str(exc):
                raise VoiceConsentRequiredError(
                    f"tenant {config.tenant_id!r} was rejected by the database's own "
                    "voice-consent check — see `onboard_tenant --voice-sample ...`"
                ) from exc
            raise
    finally:
        if owns_client:
            await active.aclose()

    # Refresh whatever's currently serving reads. Duck-typed: json mode's
    # JsonFileTenantRepository has neither method, so this is a no-op there —
    # exactly right, since content/tenants/*.json wasn't the thing that
    # changed.
    clear_tenant_cache()
    repository = get_repository()
    refresh = getattr(repository, "refresh", None)
    if refresh is not None:
        await refresh()
    else:
        invalidate = getattr(repository, "invalidate", None)
        if invalidate is not None:
            invalidate()

    return config
