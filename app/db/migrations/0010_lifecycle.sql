-- Phase 9 Part B Step B2 — bot lifecycle: the "archived" status.
--
-- Creates no table, so tests/test_migrations.py's RLS lint needs nothing new
-- here — same shape as 0007_mcp.sql / 0009_admin.sql's headers.
--
-- DECISION, recorded once so this doesn't drift into "both options half-done":
-- **purge runs on the Supabase secret key**, matching every other admin
-- write in this codebase (`create_tenant`, `save_tenant` — app/tenancy/
-- admin.py, all via `sync.py`'s `_admin_client`). This migration adds NO
-- `grant delete ... to app_backend`, deliberately:
--   * `app_backend` is the role a per-tenant JWT assumes
--     (app/db/auth.py::tenant_jwt) for ordinary per-request reads/writes.
--     Nothing in this codebase ever needs a single tenant-scoped request to
--     delete a row — widening that role's grant to include DELETE would be
--     a real increase in blast radius (any future code path minting a
--     tenant JWT could now issue a delete) for zero benefit today.
--   * The secret key needs no new grant either. CLAUDE.md's "Supabase's
--     schema default privileges quietly re-grant everything in public"
--     gotcha already documents that this project has
--     `ALTER DEFAULT PRIVILEGES ... GRANT ALL ON TABLES TO anon,
--     authenticated, service_role` — the secret key maps onto
--     `service_role`, which that statement already gives DELETE (and
--     everything else) on every table, unconditionally. The same
--     default-privilege behaviour that's normally a trap (Phase 9 Part C's
--     0011 migration has to explicitly revoke it from the *read* side) is,
--     here, exactly the access `app/tenancy/admin.py::purge_tenant` needs,
--     with nothing new to grant.

alter table public.tenants
    drop constraint if exists tenants_status_check;

alter table public.tenants
    add constraint tenants_status_check
    check (status in ('active', 'paused', 'onboarding', 'archived'));

-- ---------------------------------------------------------------------------
-- purge's Vault cleanup — the `vault` schema is not PostgREST-exposed
-- (0003_vault.sql), so even the secret key can't `DELETE FROM vault.secrets`
-- directly over PostgREST; it needs an RPC the same way every other Vault
-- access in this codebase does. `<tenant_id>::<key_name>` is the naming
-- convention 0003_vault.sql establishes — this deletes every key a tenant
-- could plausibly have (calcom_api_key, twilio_*, calcom_mcp_*, any MCP
-- server's auth_secret_ref) in one call, since purge has no reason to track
-- the exact set. Safe against a wildcard-injecting tenant_id because
-- app/tenancy/models.py's `_TENANT_ID_RE` (Phase 9 Part B Step B1) only ever
-- allows `[a-z0-9-]` — no `%`/`_` can reach this LIKE pattern.
-- ---------------------------------------------------------------------------

create or replace function public.delete_tenant_secrets(tenant_id text)
returns void
language plpgsql
security definer
set search_path = public, vault
as $$
begin
    delete from vault.secrets where name like tenant_id || '::%';
end;
$$;

revoke all on function public.delete_tenant_secrets(text) from public;
-- Operator-only, same reasoning as set_tenant_secret: never called with a
-- tenant-scoped JWT, only the Supabase secret key from purge_tenant
-- (app/tenancy/admin.py).
grant execute on function public.delete_tenant_secrets(text) to service_role;
