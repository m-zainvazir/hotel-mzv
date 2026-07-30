-- Phase 8 Step 6 — tenants.updated_at (optimistic concurrency for admin
-- writes) and a fix to the voice-consent trigger's upsert blind spot.
--
-- updated_at is set EXPLICITLY by app/tenancy/sync.py's _tenant_row, not by
-- a trigger — 0005_voice_consent.sql already demonstrates that triggers on
-- this table have teeth, and the fewer surprising ones the better. The admin
-- write path (app/tenancy/admin.py) reads this as a version token on GET,
-- requires it on PUT (If-Match), and 409s on mismatch.
--
-- No table or view is created here, so this migration needs no RLS/FORCE/
-- policy/grant statements of its own — public.tenants already has all four
-- from 0002_rls.sql, same reasoning as 0007_mcp.sql's header.

alter table public.tenants
    add column if not exists updated_at timestamptz not null default now();

-- The bug this fixes: 0005_voice_consent.sql's enforce_voice_consent()
-- comments that it "only fires when voice_id is being newly set OR changed,
-- not on every ordinary update to a tenant that already has one" — true for
-- a plain UPDATE, false under the upsert app/tenancy/sync.py::sync_tenant()
-- actually performs. `Prefer: resolution=merge-duplicates` compiles to
-- `INSERT ... ON CONFLICT DO UPDATE`, whose BEFORE INSERT pass fires with
-- tg_op = 'INSERT' — so the original trigger computed old_voice_id as null
-- even for a tenant that already has a voice_id and isn't changing it.
-- Consequence: any admin write touching an unrelated field (the greeting,
-- say) on a tenant with a cloned voice would be rejected for "no
-- voice_consents row", which is simply false. Invisible until now only
-- because no tenant has ever been cloned (CLAUDE.md).
--
-- Fixed by comparing the incoming value against the CURRENTLY STORED one via
-- a live self-select, rather than trusting `old` (which the INSERT pass
-- never populates meaningfully for an upsert). This also generalises
-- correctly to a genuine UPDATE (where a BEFORE trigger's self-select still
-- reflects the pre-update row, since the write hasn't landed yet) and to a
-- genuine INSERT of a brand-new tenant_id (the self-select finds nothing, so
-- a fresh tenant setting a voice_id from day one still requires consent,
-- unchanged from before).

create or replace function public.enforce_voice_consent()
returns trigger
language plpgsql
as $$
declare
    new_voice_id text := new.config -> 'voice' ->> 'voice_id';
    currently_stored_voice_id text;
begin
    if new_voice_id is null then
        return new;
    end if;

    select t.config -> 'voice' ->> 'voice_id'
      into currently_stored_voice_id
      from public.tenants t
     where t.tenant_id = new.tenant_id;

    if new_voice_id is distinct from currently_stored_voice_id then
        if not exists (
            select 1 from public.voice_consents where tenant_id = new.tenant_id
        ) then
            raise exception
                'tenant % has no voice_consents row — cannot set a voice_id without recorded consent',
                new.tenant_id;
        end if;
    end if;

    return new;
end;
$$;
