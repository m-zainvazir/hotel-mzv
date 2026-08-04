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
from typing import Any

import httpx

from app.config import get_settings
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
#: no per-table row count, and a purge must be auditable. Extend this tuple
#: with `("knowledge_chunks", "tenant_id")` / `("knowledge_documents",
#: "tenant_id")` at the front once Part C's tables exist; they don't yet.
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
