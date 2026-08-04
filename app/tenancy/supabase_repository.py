"""Supabase-backed tenant repository — snapshot at boot, not per-request (Phase 8).

`TenantRepository.get()` is synchronous, and its callers are graph nodes,
every native tool, prompt assembly and `resolve_tenant_id` — none of which can
`await`. Making the protocol async would be a rewrite of the whole call graph;
a blocking `httpx` call inside `get()` would stall the event loop on every
concurrent request for the ~50-200ms of a cold-cache round trip. So this class
does neither: it loads every tenant into an immutable dict once (in
`lifespan`, where `await` is available) and serves every read from that dict
with zero request-path I/O — exactly what `JsonFileTenantRepository` already
does (it caches every parsed tenant forever too), just from a different
source.

Reads the whole `tenants`/`services` registry with the Supabase **secret**
key, not a tenant-scoped JWT: there is no "list every tenant" tenant JWT (RLS
scopes by exactly one `tenant_id`), and loading the full registry at boot is a
backend-operator action, not "a tenant reading its own data" — the same
reasoning `app/tenancy/sync.py::_admin_client` already uses for the write
side. Contrast with the analytics reads (`app/db/supabase_store.py`), which
are genuinely per-tenant business data and go through the tenant JWT on
purpose, because that's what makes a future logged-in tenant's own reads use
the identical code path an operator's do.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import get_settings
from app.tenancy.models import TenantConfig
from app.tenancy.repository import TenantNotFoundError, TenantRepository

logger = logging.getLogger(__name__)

#: Pause between the two snapshot attempts. Short on purpose — this sits in
#: `lifespan`, so every second here is a second before the app serves traffic.
_RETRY_DELAY_SECONDS = 0.5


class SupabaseTenantRepository:
    """Loads every tenant from Supabase once, then serves reads from memory.

    `refresh()` never raises: a wholesale failure (no config, a network error,
    every row unusable) logs an ERROR, leaves the previous snapshot in place
    (or, on the very first load, leaves the snapshot empty so `get()` keeps
    consulting `fallback`), and sets `degraded` so `/health` can report it. A
    tenant serving stale-but-safe config is strictly better than a crashed
    boot — the same posture the checkpointer and the MCP loader already take.

    A single bad row (a hand-edited `config` blob that fails validation)
    degrades only that one tenant to its JSON fallback, mirroring
    `app/mcp/registry.py`'s per-row skip — one malformed tenant must never
    take every tenant's boot down with it.
    """

    def __init__(
        self,
        *,
        fallback: TenantRepository,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._fallback = fallback
        self._client_override = client
        self._snapshot: dict[str, TenantConfig] = {}
        self._loaded_once = False
        self.degraded = False

    # --- loading -------------------------------------------------------

    async def _fetch_rows(self, client: httpx.AsyncClient) -> Any:
        """One attempt at the registry query. Raises; the caller decides."""
        # select=*,services(*) is the embedded-resource join: `services`
        # is one of `_TENANT_COLUMNS` in app/tenancy/sync.py (so it never
        # lands in the `config` JSONB blob), but there is no `services`
        # column on `public.tenants` either — it only ever lived in
        # `public.services`, joined via the FK PostgREST detects
        # automatically (0001_schema.sql). Verified live: the embed name
        # resolves as "services" on the real project.
        response = await client.get(
            "/tenants", params={"select": "*,services(*)", "order": "tenant_id.asc"}
        )
        if response.status_code >= 400:
            raise SupabaseTenantLoadError(
                f"GET /tenants -> {response.status_code}: {response.text[:300]}"
            )
        return response.json() if response.content else []

    async def refresh(self) -> None:
        """(Re)load every tenant. Never raises — see the class docstring."""
        settings = get_settings()
        owns_client = self._client_override is None
        # The credential check only applies when we'd have to build our own
        # client — an injected `client=` (tests, same as CalcomBookingProvider
        # / TwilioNotifier / SupabaseStore) is used as-is regardless of what
        # Settings currently holds.
        if owns_client and (not settings.supabase_url or not settings.supabase_secret_key):
            logger.error(
                "SupabaseTenantRepository.refresh() called with no SUPABASE_URL / "
                "SUPABASE_SECRET_KEY configured — serving the JSON fallback"
            )
            self.degraded = True
            return

        client = self._client_override or httpx.AsyncClient(
            base_url=f"{settings.supabase_url}/rest/v1",
            headers={
                "apikey": settings.supabase_secret_key,
                "Authorization": f"Bearer {settings.supabase_secret_key}",
            },
            timeout=settings.tenant_snapshot_timeout_seconds,
        )
        try:
            try:
                rows = await self._fetch_rows(client)
            except httpx.HTTPError as exc:
                # Retry ONLY a transport-level failure, and only once. The
                # observed failure is a cold-start timeout: the first HTTPS
                # call of a fresh process pays DNS + TLS on top of the query,
                # and losing that race silently serves the JSON fallback for a
                # whole refresh interval. A second attempt reuses the
                # connection the first one just opened, so it is both cheap
                # and much more likely to succeed.
                #
                # A SupabaseTenantLoadError is deliberately NOT retried: that
                # means the server answered with a 4xx/5xx (a bad key, a
                # missing table, a broken embed), which a retry cannot fix and
                # which would only double the boot delay before the same
                # failure.
                logger.warning(
                    "tenant snapshot attempt 1 failed (%s: %s) — retrying once",
                    type(exc).__name__,
                    exc,
                )
                try:
                    await asyncio.sleep(_RETRY_DELAY_SECONDS)
                    rows = await self._fetch_rows(client)
                except (httpx.HTTPError, SupabaseTenantLoadError) as retry_exc:
                    # Log the exception TYPE, not just str(exc): httpx timeout
                    # exceptions stringify to the empty string, so the original
                    # "failed wholesale: " line named nothing at all.
                    logger.error(
                        "SupabaseTenantRepository.refresh() failed wholesale "
                        "(%s: %s) — serving the JSON fallback",
                        type(retry_exc).__name__,
                        retry_exc,
                    )
                    self.degraded = True
                    return
            except SupabaseTenantLoadError as exc:
                logger.error(
                    "SupabaseTenantRepository.refresh() failed wholesale (%s: %s) — "
                    "serving the JSON fallback",
                    type(exc).__name__,
                    exc,
                )
                self.degraded = True
                return
        finally:
            if owns_client:
                await client.aclose()

        if not isinstance(rows, list):
            logger.error("SupabaseTenantRepository.refresh(): /tenants returned a non-list body")
            self.degraded = True
            return

        snapshot: dict[str, TenantConfig] = {}
        any_row_failed = False
        for row in rows:
            tenant_id = row.get("tenant_id", "<unknown>")
            try:
                snapshot[tenant_id] = _row_to_tenant_config(row)
            except Exception:
                logger.warning(
                    "tenant %r has a Supabase config row that failed validation — "
                    "falling back to its committed JSON, if any",
                    tenant_id,
                    exc_info=True,
                )
                any_row_failed = True
                try:
                    snapshot[tenant_id] = self._fallback.get(tenant_id)
                except TenantNotFoundError:
                    logger.error(
                        "tenant %r failed to load from Supabase and has no JSON "
                        "fallback — it will not be reachable until fixed",
                        tenant_id,
                    )

        if not snapshot and rows:
            # Every row failed validation and none had a usable fallback —
            # don't replace a working snapshot (or, on first boot, hand back
            # nothing at all) with an empty one.
            logger.error(
                "SupabaseTenantRepository.refresh() produced zero usable tenants "
                "out of %d row(s) — keeping the previous snapshot",
                len(rows),
            )
            self.degraded = True
            return

        self._snapshot = snapshot
        self.degraded = any_row_failed
        self._loaded_once = True

    def invalidate(self) -> None:
        """Force the next `get()` to re-consult `fallback` until the next
        successful `refresh()`. `refresh()` is the real invalidation path —
        this exists only so `app/tenancy/admin.py` has one call
        (`getattr(repo, "invalidate", None)`) that behaves sanely regardless
        of which repository implementation is currently active."""
        self._snapshot.clear()
        self._loaded_once = False

    # --- TenantRepository protocol --------------------------------------

    def get(self, tenant_id: str) -> TenantConfig:
        config = self._snapshot.get(tenant_id)
        if config is not None:
            return config
        if not self._loaded_once:
            return self._fallback.get(tenant_id)
        raise TenantNotFoundError(f"no tenant config for {tenant_id!r} in the Supabase snapshot")

    def list_ids(self) -> list[str]:
        if self._loaded_once:
            return sorted(self._snapshot)
        return self._fallback.list_ids()

    def find_by_phone(self, phone_number: str) -> TenantConfig | None:
        wanted = _digits(phone_number)
        for config in self._iter():
            if any(_digits(n) == wanted for n in config.phone_numbers):
                return config
        return None

    def find_by_widget_key(self, widget_key: str) -> TenantConfig | None:
        for config in self._iter():
            if widget_key in config.widget_keys:
                return config
        return None

    def find_by_assistant_id(self, assistant_id: str) -> TenantConfig | None:
        if not assistant_id:
            return None
        for config in self._iter():
            if config.vapi.assistant_id == assistant_id:
                return config
        return None

    def _iter(self):
        if self._loaded_once:
            return list(self._snapshot.values())
        return [self._fallback.get(tid) for tid in self._fallback.list_ids()]


class SupabaseTenantLoadError(RuntimeError):
    pass


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _row_to_tenant_config(row: dict[str, Any]) -> TenantConfig:
    """Reverse `app/tenancy/sync.py::_tenant_row` + `_service_rows`.

    Two traps, both load-bearing:

    - `services` is one of `_TENANT_COLUMNS`, so `sync.py` strips it out of
      the `config` blob before writing — it lives only in `public.services`,
      joined back in here via the `services(*)` embed. Forgetting this join
      silently drops every service from every hydrated tenant.
    - `mcp_servers` is the opposite case: it is NOT in `_TENANT_COLUMNS`, so
      it already round-trips through the `config` JSONB blob untouched. Leave
      it there — don't reconcile it with `public.mcp_servers`, which
      `MCP_SOURCE=supabase` reads independently (Phase 6). The two are
      allowed to disagree; that's the existing design, not a bug this
      function should paper over.
    """
    config = dict(row.get("config") or {})
    services = row.get("services") or []
    merged = {
        **config,
        "tenant_id": row["tenant_id"],
        "name": row["name"],
        "trade": row["trade"],
        "timezone": row["timezone"],
        "status": row["status"],
        "phone_numbers": row.get("phone_numbers") or [],
        "widget_keys": row.get("widget_keys") or [],
        "services": [_service_row_to_dict(s) for s in services],
    }
    return TenantConfig.model_validate(merged)


def _service_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": row["slug"],
        "name": row["name"],
        "description": row.get("description", ""),
        "duration_minutes": row.get("duration_minutes", 60),
        "price_usd": row.get("price_usd"),
        "emergency": row.get("emergency", False),
        "aliases": row.get("aliases") or [],
        "event_type_id": row.get("event_type_id"),
    }
