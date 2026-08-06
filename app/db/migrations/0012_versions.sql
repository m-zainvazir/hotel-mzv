-- Phase 9.1 Feature 1 — draft/live split + deploy version history.
--
-- Same conventions as every prior migration: text primary keys, `not null
-- default ''` on non-optional strings, `enable` AND `force row level
-- security`, a `tenant_isolation` policy reading `auth.jwt() ->> 'tenant_id'`,
-- an explicit grant to `app_backend`. See plans/phase9.1.md's "Data model"
-- section for the three deliberate shapes this migration follows — repeated
-- here only where the SQL itself needs the reasoning attached.

alter table public.tenants
    add column if not exists draft_config     jsonb,
    add column if not exists draft_updated_at timestamptz;

-- draft_config is a whole-TenantConfig JSONB blob, not a mirror of the
-- relational columns sync_tenant() fans a live config out across (tenants +
-- services + mcp_servers) — those tables ARE what the runtime reads, so one
-- inert column is the only way to guarantee a draft can't leak live.

create table public.tenant_versions (
    id             text primary key,
    tenant_id      text not null references public.tenants (tenant_id) on delete cascade,
    version_number integer not null,
    config         jsonb   not null,
    note           text    not null default '',
    deployed_by    text    not null default '',
    deployed_at    timestamptz not null default now(),
    is_live        boolean not null default false,
    unique (tenant_id, version_number)
);

-- is_live + a partial unique index, not a tenants.live_version_id FK: a
-- pointer column would make tenants and tenant_versions mutually
-- referential, which fights both purge_tenant's FK-ordered deletes and
-- "delete an old version". This gives the same one-live-per-tenant
-- guarantee with no cycle.
create unique index tenant_versions_one_live_idx
    on public.tenant_versions (tenant_id) where is_live;

create index tenant_versions_tenant_idx
    on public.tenant_versions (tenant_id, version_number desc);

alter table public.tenant_versions enable row level security;
alter table public.tenant_versions force row level security;
drop policy if exists tenant_isolation on public.tenant_versions;
create policy tenant_isolation on public.tenant_versions
    for all
    using (tenant_id = (auth.jwt() ->> 'tenant_id'))
    with check (tenant_id = (auth.jwt() ->> 'tenant_id'));

-- No delete grant here, matching 0010_lifecycle.sql's reasoning: version
-- deletion (app/tenancy/admin.py::delete_version) runs on the Supabase
-- secret key (service_role), which already holds DELETE via the project's
-- own ALTER DEFAULT PRIVILEGES. app_backend only ever needs to read and
-- write its own tenant's versions through the ordinary per-request JWT path.
grant select, insert, update on public.tenant_versions to app_backend;

-- Same default-privileges trap 0011_knowledge.sql's header documents: a
-- freshly created table silently inherits full access for anon/authenticated
-- too. tenant_versions holds a full historical TenantConfig per row —
-- exactly as sensitive as the live config itself.
revoke all on public.tenant_versions from anon, authenticated;
