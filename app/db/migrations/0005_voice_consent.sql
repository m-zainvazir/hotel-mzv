-- Phase 4 Step 8 — enforce voice consent at the database layer too.
--
-- CLAUDE.md convention #6 has no exceptions: a tenant's voice_id may only be
-- set once a voice_consents row exists for that tenant. Enforced in
-- app/tenancy/voice.py (the only code path that should ever call Cartesia's
-- clone endpoint) AND here, because "no exceptions" has to survive someone
-- running SQL by hand.
--
-- voice_id lives at config -> 'voice' ->> 'voice_id' — the tenants.config
-- JSONB blob (see 0001_schema.sql) — since the tenant *read* path stays on
-- content/tenants/*.json for Phase 4; this table is write-only via
-- scripts/onboard_tenant.py / scripts/sync_tenants.py until that flips.
--
-- The trigger only fires when voice_id is being newly set OR changed, not
-- on every ordinary update to a tenant that already has one — otherwise an
-- unrelated edit (e.g. changing hours) would be blocked by this check too.

create or replace function public.enforce_voice_consent()
returns trigger
language plpgsql
as $$
declare
    new_voice_id text := new.config -> 'voice' ->> 'voice_id';
    old_voice_id text := case
        when tg_op = 'INSERT' then null
        else old.config -> 'voice' ->> 'voice_id'
    end;
begin
    if new_voice_id is not null and new_voice_id is distinct from old_voice_id then
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

drop trigger if exists trg_enforce_voice_consent on public.tenants;
create trigger trg_enforce_voice_consent
    before insert or update on public.tenants
    for each row
    execute function public.enforce_voice_consent();
