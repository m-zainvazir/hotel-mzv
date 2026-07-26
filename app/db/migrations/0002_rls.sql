-- Phase 4 — Row Level Security.
--
-- Two things make RLS real rather than decorative here:
--   1. FORCE, not just ENABLE. Tables created via the dashboard SQL editor
--      are owned by `postgres`, and ENABLE ROW LEVEL SECURITY alone does not
--      apply to the table owner — FORCE closes that.
--   2. A dedicated `app_backend` role, not `authenticated`. `authenticated`
--      is GoTrue's role for end users; the moment a customer-facing
--      dashboard exists on Supabase Auth, those users would inherit every
--      grant written for the backend. `app_backend` is `nologin` and only
--      reachable by a JWT the backend itself mints (app/db/auth.py).
--
-- RLS sits *on top of* GRANTs, not instead of them: a perfect policy with no
-- GRANT is a 403 that reads like an auth bug, and ENABLE with no policy is
-- deny-all that surfaces as an empty read indistinguishable from "no data
-- yet" — the same silent-empty failure mode as plan Risk 6 (Phase 3). Both
-- are why every table below gets an explicit GRANT alongside its policy.
--
-- Policies use DROP POLICY IF EXISTS first so this file is safe to re-run
-- from the dashboard SQL editor while iterating.

do $$
begin
    if not exists (select from pg_roles where rolname = 'app_backend') then
        create role app_backend nologin;
    end if;
end
$$;

-- Lets PostgREST switch into this role when it sees "role": "app_backend"
-- in a JWT signed with the project's own secret (see app/db/auth.py).
grant app_backend to authenticator;

grant usage on schema public to app_backend;

-- ---------------------------------------------------------------------------
-- One policy per table: a tenant-scoped JWT (auth.jwt() ->> 'tenant_id') may
-- only touch its own rows, for every operation. No table needs a broader
-- policy — write-across-tenants (onboarding, sync_tenants.py) goes through
-- the secret/service_role key instead, which bypasses RLS entirely and is
-- never used for a single-tenant-scoped request.
-- ---------------------------------------------------------------------------

alter table public.tenants enable row level security;
alter table public.tenants force row level security;
drop policy if exists tenant_isolation on public.tenants;
create policy tenant_isolation on public.tenants
    for all
    using (tenant_id = (auth.jwt() ->> 'tenant_id'))
    with check (tenant_id = (auth.jwt() ->> 'tenant_id'));
grant select, insert, update on public.tenants to app_backend;

alter table public.services enable row level security;
alter table public.services force row level security;
drop policy if exists tenant_isolation on public.services;
create policy tenant_isolation on public.services
    for all
    using (tenant_id = (auth.jwt() ->> 'tenant_id'))
    with check (tenant_id = (auth.jwt() ->> 'tenant_id'));
grant select, insert, update on public.services to app_backend;

alter table public.jobs enable row level security;
alter table public.jobs force row level security;
drop policy if exists tenant_isolation on public.jobs;
create policy tenant_isolation on public.jobs
    for all
    using (tenant_id = (auth.jwt() ->> 'tenant_id'))
    with check (tenant_id = (auth.jwt() ->> 'tenant_id'));
grant select, insert, update on public.jobs to app_backend;

alter table public.calls enable row level security;
alter table public.calls force row level security;
drop policy if exists tenant_isolation on public.calls;
create policy tenant_isolation on public.calls
    for all
    using (tenant_id = (auth.jwt() ->> 'tenant_id'))
    with check (tenant_id = (auth.jwt() ->> 'tenant_id'));
grant select, insert, update on public.calls to app_backend;

alter table public.messages enable row level security;
alter table public.messages force row level security;
drop policy if exists tenant_isolation on public.messages;
create policy tenant_isolation on public.messages
    for all
    using (tenant_id = (auth.jwt() ->> 'tenant_id'))
    with check (tenant_id = (auth.jwt() ->> 'tenant_id'));
grant select, insert, update on public.messages to app_backend;

alter table public.escalations enable row level security;
alter table public.escalations force row level security;
drop policy if exists tenant_isolation on public.escalations;
create policy tenant_isolation on public.escalations
    for all
    using (tenant_id = (auth.jwt() ->> 'tenant_id'))
    with check (tenant_id = (auth.jwt() ->> 'tenant_id'));
grant select, insert, update on public.escalations to app_backend;

alter table public.mcp_servers enable row level security;
alter table public.mcp_servers force row level security;
drop policy if exists tenant_isolation on public.mcp_servers;
create policy tenant_isolation on public.mcp_servers
    for all
    using (tenant_id = (auth.jwt() ->> 'tenant_id'))
    with check (tenant_id = (auth.jwt() ->> 'tenant_id'));
grant select, insert, update on public.mcp_servers to app_backend;

alter table public.voice_consents enable row level security;
alter table public.voice_consents force row level security;
drop policy if exists tenant_isolation on public.voice_consents;
create policy tenant_isolation on public.voice_consents
    for all
    using (tenant_id = (auth.jwt() ->> 'tenant_id'))
    with check (tenant_id = (auth.jwt() ->> 'tenant_id'));
grant select, insert, update on public.voice_consents to app_backend;
