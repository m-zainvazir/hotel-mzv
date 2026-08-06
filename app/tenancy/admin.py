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

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import get_settings
from app.db.models import TenantVersion
from app.tenancy.loader import get_tenant_config, refresh_tenant_repository
from app.tenancy.models import TenantConfig
from app.tenancy.repository import TenantNotFoundError
from app.tenancy.secrets import TenantSecretError, delete_tenant_secrets
from app.tenancy.sync import TenantSyncError, _admin_client, sync_tenant

logger = logging.getLogger(__name__)

#: FK order for `purge_tenant` (Phase 9 Part B) — most of these tables have
#: no `on delete cascade` from `tenants` (app/db/migrations/0001_schema.sql),
#: so a naive `DELETE /tenants` is rejected for any bot that has ever taken a
#: call. `services` / `mcp_servers` / `voice_consents` DO cascade already,
#: but are still deleted explicitly and in order here — cascade would give
#: no per-table row count, and a purge must be auditable. `tenant_versions`
#: (Phase 9.1) sits immediately before `tenants` — same class of gap this
#: tuple already closed once for `knowledge_chunks`/`knowledge_documents`:
#: the FK cascades regardless, but the per-table counts logged for audit
#: would under-report without it.
_PURGE_TABLES: tuple[str, ...] = (
    "knowledge_chunks",
    "knowledge_documents",
    "chat_messages",
    "chat_sessions",
    "escalations",
    "messages",
    "jobs",
    "calls",
    "services",
    "mcp_servers",
    "voice_consents",
    "tenant_versions",
    "tenants",
)

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


class TenantAlreadyExistsError(RuntimeError):
    """`create_tenant` refuses to overwrite an existing `tenant_id` — a
    duplicate is 409-worthy, never a silent upsert the way `save_tenant`'s
    is (that one is *only* ever reached for a tenant the caller already
    knows exists, via `PUT /tenants/{tenant_id}`)."""


class TenantNotArchivedError(RuntimeError):
    """`purge_tenant` refuses to run against anything but an already-archived
    tenant — enforced here, not just at the route (`app/channels/admin.py`),
    since this is the single most destructive operation in the codebase
    (plan §9 Risk 5) and deserves defense in depth, not one gate."""


class TenantPurgeError(RuntimeError):
    """A row-deletion request failed partway through `purge_tenant` — surfaced
    as-is (never silently swallowed) since a partial purge leaves orphaned
    data an operator needs to know about."""


class VoiceConsentRequiredError(RuntimeError):
    """Raised by both the pre-check (before any write reaches Postgres) and
    by mapping a PostgREST error body naming `voice_consents` — covers the
    race between the two, with the same actionable message either way."""


class NoDraftError(RuntimeError):
    """`deploy_tenant` found no `draft_config` to publish — 409 (nothing to
    deploy), never a silent no-op."""


class VersionNotFoundError(LookupError):
    """`switch_to_version`/`delete_version` referenced a `tenant_versions`
    row that doesn't exist (or belongs to a different tenant)."""


class LiveVersionDeleteError(RuntimeError):
    """`delete_version` refuses to delete the currently-live version — the
    partial unique index (`0012_versions.sql`) would let this happen at the
    database level, but a tenant with no live version is a worse bug than
    refusing the request."""


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


async def _current_row(tenant_id: str, client: httpx.AsyncClient) -> dict[str, Any] | None:
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
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
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
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
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

    await refresh_tenant_repository()
    return config


# --- draft / deploy / version history (Phase 9.1) ---------------------------
#
# `save_tenant` above stays the ONLY function that touches sync_tenant()'s
# fan-out (tenants + services + mcp_servers) and calls
# refresh_tenant_repository() — that's what the runtime actually reads.
# Everything below either writes the inert `draft_config`/`draft_updated_at`
# columns (never reaching sync_tenant/refresh), or reaches live by calling
# save_tenant itself (deploy_tenant, switch_to_version) — never a second,
# parallel write path. `refresh_tenant_repository()` fires on Deploy and
# switch, and only there — the opposite of the phantom edit Phase 8 fixed.


async def _current_draft_row(tenant_id: str, client: httpx.AsyncClient) -> dict[str, Any] | None:
    response = await client.get(
        "/tenants",
        params={
            "tenant_id": f"eq.{tenant_id}",
            "select": "draft_config,draft_updated_at",
            "limit": "1",
        },
    )
    if response.status_code >= 400 or not response.content:
        return None
    rows = response.json()
    return rows[0] if rows else None


async def get_draft(
    tenant_id: str, *, client: httpx.AsyncClient | None = None
) -> tuple[TenantConfig | None, str | None]:
    """`(None, None)` when there's no unpublished draft — including when
    Supabase isn't configured (dev/test's `TENANT_SOURCE=json`), the same
    "skip entirely" reading `get_tenant_version` gives that case."""
    settings = get_settings()
    owns_client = client is None
    if owns_client and (not settings.supabase_url or not settings.supabase_secret_key):
        return None, None
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
    try:
        row = await _current_draft_row(tenant_id, active)
    finally:
        if owns_client:
            await active.aclose()
    if not row or not row.get("draft_config"):
        return None, None
    return TenantConfig.model_validate(row["draft_config"]), row.get("draft_updated_at")


async def save_draft(
    config: TenantConfig,
    *,
    expected_version: str | None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Write `config` into `tenants.draft_config` — nothing else. No
    `sync_tenant()` call, no `refresh_tenant_repository()` call: that silence
    is the entire safety argument for why a draft can never leak live.
    Returns the new `draft_updated_at`, the version token the next save's
    `If-Match` compares against.
    """
    settings = get_settings()
    owns_client = client is None
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
    try:
        if expected_version is not None:
            current = await _current_draft_row(config.tenant_id, active)
            stored_version = current.get("draft_updated_at") if current else None
            if stored_version != expected_version:
                raise VersionConflictError(
                    f"tenant {config.tenant_id!r}'s draft was changed by someone else since "
                    "you loaded it — reload and re-apply your edit"
                )

        now = datetime.now(UTC).isoformat()
        response = await active.post(
            "/tenants",
            params={"on_conflict": "tenant_id"},
            json={
                "tenant_id": config.tenant_id,
                "draft_config": config.model_dump(mode="json"),
                "draft_updated_at": now,
            },
        )
        if response.status_code >= 400:
            raise TenantSyncError(
                f"could not save draft for {config.tenant_id!r}: {response.status_code} "
                f"{response.text[:300]}"
            )
    finally:
        if owns_client:
            await active.aclose()
    return now


async def discard_draft(tenant_id: str, *, client: httpx.AsyncClient | None = None) -> None:
    settings = get_settings()
    owns_client = client is None
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
    try:
        response = await active.patch(
            "/tenants",
            params={"tenant_id": f"eq.{tenant_id}"},
            json={"draft_config": None, "draft_updated_at": None},
        )
        if response.status_code >= 400:
            raise TenantSyncError(
                f"could not discard draft for {tenant_id!r}: {response.status_code} "
                f"{response.text[:300]}"
            )
    finally:
        if owns_client:
            await active.aclose()


async def _max_version_number(tenant_id: str, client: httpx.AsyncClient) -> int:
    response = await client.get(
        "/tenant_versions",
        params={
            "tenant_id": f"eq.{tenant_id}",
            "select": "version_number",
            "order": "version_number.desc",
            "limit": "1",
        },
    )
    if response.status_code >= 400 or not response.content:
        return 0
    rows = response.json()
    return rows[0]["version_number"] if rows else 0


async def _unset_live_version(tenant_id: str, client: httpx.AsyncClient) -> None:
    response = await client.patch(
        "/tenant_versions",
        params={"tenant_id": f"eq.{tenant_id}", "is_live": "eq.true"},
        json={"is_live": False},
    )
    if response.status_code >= 400:
        raise TenantSyncError(
            f"could not clear the previous live version for {tenant_id!r}: "
            f"{response.status_code} {response.text[:300]}"
        )


async def _insert_version(version: TenantVersion, client: httpx.AsyncClient) -> None:
    response = await client.post("/tenant_versions", json=version.model_dump(mode="json"))
    if response.status_code >= 400:
        raise TenantSyncError(
            f"could not record deploy version for {version.tenant_id!r}: "
            f"{response.status_code} {response.text[:300]}"
        )


async def _get_version_row(
    tenant_id: str, version_id: str, client: httpx.AsyncClient
) -> dict[str, Any] | None:
    response = await client.get(
        "/tenant_versions",
        params={"id": f"eq.{version_id}", "tenant_id": f"eq.{tenant_id}", "limit": "1"},
    )
    if response.status_code >= 400 or not response.content:
        return None
    rows = response.json()
    return rows[0] if rows else None


async def deploy_tenant(
    tenant_id: str,
    *,
    note: str = "",
    deployed_by: str = "",
    client: httpx.AsyncClient | None = None,
) -> TenantVersion:
    """Publish the current draft: validate -> write live (reusing
    `save_tenant`'s consent gate + `sync_tenant` fan-out + repository
    refresh) -> record an immutable version row -> clear the draft.

    Live write (step 3) happens BEFORE the version row is inserted (step 4)
    deliberately — a failed live write must never leave a version row
    claiming to be live. `TenantConfig.model_validate` re-validates the
    stored draft rather than trusting whatever passed validation at save
    time, so a schema change between save and deploy surfaces as a clean
    `ValidationError` (the route maps it to 422), never a broken live bot.
    """
    settings = get_settings()
    owns_client = client is None
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
    try:
        draft_row = await _current_draft_row(tenant_id, active)
        raw_draft = draft_row.get("draft_config") if draft_row else None
        if not raw_draft:
            raise NoDraftError(f"tenant {tenant_id!r} has no draft to deploy")

        config = TenantConfig.model_validate(raw_draft)

        await save_tenant(config, expected_version=None, client=active)

        version = TenantVersion(
            tenant_id=tenant_id,
            version_number=await _max_version_number(tenant_id, active) + 1,
            config=config.model_dump(mode="json"),
            note=note,
            deployed_by=deployed_by,
            is_live=True,
        )
        await _unset_live_version(tenant_id, active)
        await _insert_version(version, active)

        response = await active.patch(
            "/tenants",
            params={"tenant_id": f"eq.{tenant_id}"},
            json={"draft_config": None, "draft_updated_at": None},
        )
        if response.status_code >= 400:
            raise TenantSyncError(
                f"deployed {tenant_id!r} but could not clear its draft: "
                f"{response.status_code} {response.text[:300]}"
            )
    finally:
        if owns_client:
            await active.aclose()
    return version


async def get_live_version(
    tenant_id: str, *, client: httpx.AsyncClient | None = None
) -> TenantVersion | None:
    """The currently-live `tenant_versions` row, or `None` — queried
    directly by `is_live` rather than assuming it's whatever has the
    highest `version_number`, since `switch_to_version` can make an OLDER
    version live again without touching version numbers at all."""
    settings = get_settings()
    owns_client = client is None
    if owns_client and (not settings.supabase_url or not settings.supabase_secret_key):
        return None
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
    try:
        response = await active.get(
            "/tenant_versions",
            params={"tenant_id": f"eq.{tenant_id}", "is_live": "eq.true", "limit": "1"},
        )
        if response.status_code >= 400 or not response.content:
            return None
        rows = response.json()
    finally:
        if owns_client:
            await active.aclose()
    return TenantVersion.model_validate(rows[0]) if rows else None


async def list_versions(
    tenant_id: str, *, limit: int = 50, client: httpx.AsyncClient | None = None
) -> list[TenantVersion]:
    settings = get_settings()
    owns_client = client is None
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
    try:
        response = await active.get(
            "/tenant_versions",
            params={
                "tenant_id": f"eq.{tenant_id}",
                "order": "version_number.desc",
                "limit": str(limit),
            },
        )
        if response.status_code >= 400:
            raise TenantSyncError(
                f"could not list versions for {tenant_id!r}: {response.status_code} "
                f"{response.text[:300]}"
            )
        rows = response.json() if response.content else []
    finally:
        if owns_client:
            await active.aclose()
    return [TenantVersion.model_validate(row) for row in rows]


async def switch_to_version(
    tenant_id: str, version_id: str, *, client: httpx.AsyncClient | None = None
) -> TenantVersion:
    """Rollback/roll-forward: validate -> write live -> flip `is_live`. No
    new row, no version number burned — unlike `deploy_tenant`, this is
    "make an existing snapshot live again," not a new publish."""
    settings = get_settings()
    owns_client = client is None
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
    try:
        row = await _get_version_row(tenant_id, version_id, active)
        if row is None:
            raise VersionNotFoundError(f"no version {version_id!r} for tenant {tenant_id!r}")

        config = TenantConfig.model_validate(row["config"])
        await save_tenant(config, expected_version=None, client=active)

        await _unset_live_version(tenant_id, active)
        response = await active.patch(
            "/tenant_versions",
            params={"id": f"eq.{version_id}"},
            json={"is_live": True},
        )
        if response.status_code >= 400:
            raise TenantSyncError(
                f"could not mark version {version_id!r} live: {response.status_code} "
                f"{response.text[:300]}"
            )
    finally:
        if owns_client:
            await active.aclose()
    return TenantVersion.model_validate({**row, "is_live": True})


async def delete_version(
    tenant_id: str, version_id: str, *, client: httpx.AsyncClient | None = None
) -> None:
    """409s on the live version (`LiveVersionDeleteError`) — the partial
    unique index + `on delete cascade` make the FK side safe regardless, but
    a tenant with no live version is a worse bug than refusing the request."""
    settings = get_settings()
    owns_client = client is None
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
    try:
        row = await _get_version_row(tenant_id, version_id, active)
        if row is None:
            raise VersionNotFoundError(f"no version {version_id!r} for tenant {tenant_id!r}")
        if row.get("is_live"):
            raise LiveVersionDeleteError(
                f"version {version_id!r} is the live version for {tenant_id!r} — switch "
                "another version live first"
            )
        response = await active.delete("/tenant_versions", params={"id": f"eq.{version_id}"})
        if response.status_code >= 400:
            raise TenantSyncError(
                f"could not delete version {version_id!r}: {response.status_code} "
                f"{response.text[:300]}"
            )
    finally:
        if owns_client:
            await active.aclose()


async def create_tenant(
    config: TenantConfig, *, client: httpx.AsyncClient | None = None
) -> TenantConfig:
    """Create a brand-new tenant (Phase 9 Part B) — the panel's "+ New bot".

    Refuses an existing `tenant_id` (`TenantAlreadyExistsError`, mapped to
    409 by the route) rather than silently upserting over it, the opposite
    of `save_tenant`'s job. Writes as `status: "onboarding"` first, then as
    whatever final status `config` itself carries (skipped if that's already
    `"onboarding"`) — matching `onboard_tenant.py`'s ordering, so a
    half-created bot never looks live by accident even for the brief window
    between the two writes. The admin route always passes `status="active"`
    for a panel-created bot; `set_tenant_status` is the primitive for
    changing it again later.

    Deploys immediately (Phase 9.1): writes live AND records version 1. A
    bot that existed only as a draft would be invisible to every read path
    — `get_tenant_config`, the Versions tab, the Test Agent link — and that
    would be a confusing "did creation even work?" state for an operator.
    """
    settings = get_settings()
    owns_client = client is None
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
    try:
        existing = await _current_row(config.tenant_id, active)
        if existing is not None:
            raise TenantAlreadyExistsError(f"tenant {config.tenant_id!r} already exists")

        onboarding = config.model_copy(update={"status": "onboarding"})
        await sync_tenant(onboarding, client=active)

        if config.status != "onboarding":
            await sync_tenant(config, client=active)

        await _insert_version(
            TenantVersion(
                tenant_id=config.tenant_id,
                version_number=1,
                config=config.model_dump(mode="json"),
                note="initial creation",
                is_live=True,
            ),
            active,
        )
    finally:
        if owns_client:
            await active.aclose()

    await refresh_tenant_repository()
    return config


async def set_tenant_status(
    tenant_id: str, status: str, *, client: httpx.AsyncClient | None = None
) -> TenantConfig:
    """Archive / restore (Phase 9 Part B) — a pure status flip, nothing else
    about the tenant changes. Deliberately goes through `sync_tenant`
    directly rather than `save_tenant`: no optimistic-concurrency version
    check and no voice-consent gate apply to a status-only write, and both
    would just add friction to what's meant to be a one-click panel action.
    `TenantConfig.model_validate` (via `model_copy` + Pydantic's frozen
    model semantics) is what actually enforces `status` is one of the four
    legal values — an illegal one raises before any write is attempted.
    """
    current = get_tenant_config(tenant_id)
    updated = TenantConfig.model_validate({**current.model_dump(mode="json"), "status": status})

    settings = get_settings()
    owns_client = client is None
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
    try:
        await sync_tenant(updated, client=active)
    finally:
        if owns_client:
            await active.aclose()

    await refresh_tenant_repository()
    return updated


async def purge_tenant(
    tenant_id: str, *, client: httpx.AsyncClient | None = None
) -> dict[str, int]:
    """Irreversibly delete every row this tenant has (Phase 9 Part B) —
    the most destructive operation in the codebase (plan §9 Risk 5).

    Refuses unless the tenant is already `"archived"` (`TenantNotArchivedError`,
    mapped to 409 by the route, which also enforces the typed-confirmation
    precondition on the request body — this function only ever sees a bare
    `tenant_id`). Deletes in FK order (`_PURGE_TABLES`) since most of these
    tables have no `on delete cascade` from `tenants`, then best-effort
    cleans up Vault secrets, the Vapi assistant, and a committed
    `content/tenants/<id>.json` if one exists — none of those three block on
    each other or abort if one fails, since the row deletion above is
    already irreversible by that point; each failure is logged instead.
    Returns per-table row counts, and logs them too — a purge must be
    auditable.
    """
    try:
        current = get_tenant_config(tenant_id)
    except TenantNotFoundError:
        raise

    if current.status != "archived":
        raise TenantNotArchivedError(
            f"tenant {tenant_id!r} must be archived before it can be purged "
            f"(current status: {current.status!r})"
        )

    settings = get_settings()
    owns_client = client is None
    active = client or _admin_client(settings, timeout=settings.supabase_timeout_seconds)
    counts: dict[str, int] = {}
    try:
        for table in _PURGE_TABLES:
            counts[table] = await _delete_rows(active, table, tenant_id)
    finally:
        if owns_client:
            await active.aclose()

    logger.info("purged tenant %s: %s", tenant_id, counts)

    try:
        await delete_tenant_secrets(tenant_id)
    except TenantSecretError:
        logger.warning("purge %s: could not delete Vault secrets", tenant_id, exc_info=True)

    if current.vapi.assistant_id:
        try:
            await asyncio.to_thread(_delete_vapi_assistant, current.vapi.assistant_id)
        except Exception:
            logger.warning("purge %s: could not delete Vapi assistant", tenant_id, exc_info=True)

    json_path = settings.tenant_data_dir / f"{tenant_id}.json"
    if json_path.is_file():
        try:
            json_path.unlink()
        except OSError:
            logger.warning("purge %s: could not remove %s", tenant_id, json_path, exc_info=True)

    await refresh_tenant_repository()
    return counts


async def _delete_rows(client: httpx.AsyncClient, table: str, tenant_id: str) -> int:
    response = await client.delete(
        f"/{table}",
        params={"tenant_id": f"eq.{tenant_id}"},
        headers={"Prefer": "return=representation"},
    )
    if response.status_code >= 400:
        raise TenantPurgeError(
            f"could not delete {table} for {tenant_id!r}: {response.status_code} "
            f"{response.text[:300]}"
        )
    if not response.content:
        return 0
    rows = response.json()
    return len(rows) if isinstance(rows, list) else 0


def _delete_vapi_assistant(assistant_id: str) -> None:
    # Local import: app.channels.vapi_provisioning is otherwise unrelated to
    # this module's day-to-day (JWT-scoped analytics reads, secret-key
    # writes), and its own import chain has no business loading on every
    # admin write — only the rare purge that actually has a Vapi assistant
    # to clean up.
    from app.channels.vapi_provisioning import VapiClient

    with VapiClient() as vapi:
        vapi.delete_assistant(assistant_id)
