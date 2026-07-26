-- Phase 4 — per-tenant secrets via Supabase Vault.
--
-- The `vault` schema is not PostgREST-exposed (confirm live — plan
-- verification item 6) and must stay that way: `vault.decrypted_secrets`
-- decrypts on read, so exposing it directly would hand out every tenant's
-- Cal.com/Twilio credentials to anything holding an anon key.
--
-- Naming convention for secrets: "<tenant_id>::<key_name>", e.g.
-- "hotel-mzv::calcom_api_key". The RPC below derives `tenant_id` from the
-- caller's own JWT claim rather than accepting it as a parameter — a
-- tenant-scoped JWT can therefore only ever read its own tenant's secrets,
-- no matter what `key_name` it passes. This is the enforcement point that
-- keeps per-tenant secrets actually isolated; the RLS policies in
-- 0002_rls.sql don't cover Vault at all, since vault.secrets carries no
-- tenant_id column of its own.
--
-- Writing secrets (onboarding, scripts/onboard_tenant.py) goes through
-- `vault.create_secret()` / `vault.update_secret()` directly on the
-- secret/service_role admin connection — those vault functions are usable by
-- that role by default and need no wrapper, since the admin path
-- intentionally bypasses this per-tenant restriction.

create or replace function public.get_tenant_secret(key_name text)
returns text
language plpgsql
security definer
set search_path = public, vault
as $$
declare
    requesting_tenant text := auth.jwt() ->> 'tenant_id';
    result text;
begin
    if requesting_tenant is null or key_name is null then
        return null;
    end if;

    select decrypted_secret
      into result
      from vault.decrypted_secrets
     where name = requesting_tenant || '::' || key_name
     limit 1;

    return result;
end;
$$;

-- No default PUBLIC execute — only the backend's own scoped role may call
-- this, and only ever for the tenant named in its own JWT.
revoke all on function public.get_tenant_secret(text) from public;
grant execute on function public.get_tenant_secret(text) to app_backend;

-- Admin-only write path (Phase 4 Step 9's onboarding script). Takes
-- `tenant_id` as an explicit parameter — unlike get_tenant_secret, this is
-- never called with a tenant-scoped JWT, only with the Supabase secret key,
-- so there is no caller-supplied tenant_id to distrust here. Lets
-- scripts/onboard_tenant.py write per-tenant secrets over PostgREST without
-- a direct psycopg connection: the plan's "two doors" design reserves
-- psycopg for the LangGraph checkpointer only (app/db/checkpointer.py) —
-- everything else, including this, goes through PostgREST.
--
-- Verified live against this project's vault schema (2026-07-26):
--   vault.create_secret(new_secret text, new_name text DEFAULT NULL, ...) returns uuid
--   vault.update_secret(secret_id uuid, new_secret text DEFAULT NULL, ...) returns void
create or replace function public.set_tenant_secret(
    tenant_id text, key_name text, secret_value text
)
returns void
language plpgsql
security definer
set search_path = public, vault
as $$
declare
    secret_name text := tenant_id || '::' || key_name;
    existing_id uuid;
begin
    select id into existing_id from vault.secrets where name = secret_name;
    if existing_id is not null then
        perform vault.update_secret(existing_id, secret_value);
    else
        perform vault.create_secret(secret_value, secret_name, 'set via onboarding');
    end if;
end;
$$;

revoke all on function public.set_tenant_secret(text, text, text) from public;
-- Deliberately NOT granted to app_backend: a tenant-scoped request may only
-- ever READ its own secret (get_tenant_secret above), never write any
-- tenant's. service_role already has broad implicit privileges on public
-- (verified live — it could call get_tenant_secret with no explicit grant),
-- but this grant is added anyway so the intent is explicit, not implicit.
grant execute on function public.set_tenant_secret(text, text, text) to service_role;
