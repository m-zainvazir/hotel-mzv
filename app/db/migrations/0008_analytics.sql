-- Phase 8 — analytics views for the admin dashboard.
--
-- There is zero aggregation anywhere in the schema before this file: no
-- count/sum/group-by on any table, no view, no aggregate function. Three
-- ways to get one were considered and two were rejected:
--   * Python-side aggregation over the existing alist_* store methods —
--     alist_calls(tenant_id) has no limit, no time filter, and selects
--     `transcript`, so "calls this week" would pull every transcript across
--     the wire on every dashboard load. Unbounded latency and a PII problem
--     at once, and chat volume is uncomputable this way at all (there is no
--     "list sessions for a tenant" method).
--   * PostgREST's own aggregate syntax (select=count()) — gated by
--     `db-aggregates-enabled`, shipped disabled-by-default on some Supabase
--     releases, failing as an opaque 400. Making the dashboard's core query
--     depend on a server setting that fails silently-wrong is the wrong
--     dependency.
--   * SQL views, read through the existing tenant-scoped JWT. Chosen.
--
-- WITH (security_invoker = true) IS NOT OPTIONAL. A plain view in `public`
-- is exposed by PostgREST automatically and runs with the OWNER's
-- privileges — it does NOT apply the underlying tables' RLS. A
-- daily_call_stats view without this option hands every tenant's call
-- volume to anything holding the anon key. tests/test_migrations.py's lint
-- is extended in this phase to fail any new view missing it,
-- the same reasoning the original table lint was written under ("someone
-- added a table and forgot RLS"), applied to a new object type.
--
-- Every view/RPC below is granted to app_backend ONLY — never to
-- `authenticated`. 0002_rls.sql chose app_backend over authenticated
-- specifically so that the moment a customer-facing dashboard exists on
-- Supabase Auth, those users would not inherit every grant written for the
-- backend. When per-tenant login lands (plans/phase10.md item 14), the
-- browser still never talks to PostgREST directly — it talks to
-- /admin/api, which mints its own app_backend tenant JWT server-side.
-- "Just let the browser use supabase-js with the user's own JWT" is the
-- one-line shortcut that would undo this; don't take it.
--
-- Cross-tenant rollups (the operator's "all tenants" landing page) are a
-- per-tenant loop over these views using each tenant's own JWT, never the
-- secret key — see plans/phase8.md "Cross-tenant rollups". That is what
-- keeps a future logged-in tenant's own read of its own data on the
-- identical code path an operator's read already uses.

-- ---------------------------------------------------------------------------
-- daily_call_stats — deliberately no transcript/recording_url column. The
-- list-shaped analytics surface is PII-free by construction; a transcript
-- is only ever reached one call at a time, via an explicit operator action
-- (GET /admin/api/tenants/{tid}/calls/{call_id}), never a bulk read.
-- ---------------------------------------------------------------------------

create view public.daily_call_stats
with (security_invoker = true)
as
select
    tenant_id,
    date_trunc('day', created_at) as day,
    count(*) as calls,
    coalesce(sum(duration_seconds), 0) as total_seconds,
    coalesce(avg(duration_seconds), 0) as avg_seconds,
    coalesce(sum(cost_usd), 0) as cost_usd,
    count(*) filter (where ended_reason like 'error%') as error_count
from public.calls
group by tenant_id, date_trunc('day', created_at);

grant select on public.daily_call_stats to app_backend;

-- ---------------------------------------------------------------------------
-- daily_job_stats — bookings per day, split by status and channel so the
-- dashboard can show "booking rate" (jobs / conversations) and distinguish
-- a cancelled booking from a completed one.
-- ---------------------------------------------------------------------------

create view public.daily_job_stats
with (security_invoker = true)
as
select
    tenant_id,
    date_trunc('day', created_at) as day,
    status,
    channel,
    count(*) as jobs
from public.jobs
group by tenant_id, date_trunc('day', created_at), status, channel;

grant select on public.daily_job_stats to app_backend;

-- ---------------------------------------------------------------------------
-- daily_chat_stats — the only reason chat volume becomes computable at all;
-- app/db/store.py's ChatLog has no "list sessions for a tenant" method, so
-- without this view chat activity is invisible to analytics entirely.
-- full outer join is safe under RLS: each side is independently scoped to
-- the calling tenant before the join runs (security_invoker), so the join
-- can never combine two different tenants' rows.
-- ---------------------------------------------------------------------------

create view public.daily_chat_stats
with (security_invoker = true)
as
select
    coalesce(s.tenant_id, m.tenant_id) as tenant_id,
    coalesce(s.day, m.day) as day,
    coalesce(s.sessions, 0) as sessions,
    coalesce(m.messages, 0) as messages
from (
    select tenant_id, date_trunc('day', started_at) as day, count(*) as sessions
    from public.chat_sessions
    group by tenant_id, date_trunc('day', started_at)
) s
full outer join (
    select tenant_id, date_trunc('day', created_at) as day, count(*) as messages
    from public.chat_messages
    group by tenant_id, date_trunc('day', created_at)
) m
    on s.tenant_id = m.tenant_id and s.day = m.day;

grant select on public.daily_chat_stats to app_backend;

-- ---------------------------------------------------------------------------
-- daily_escalation_stats
-- ---------------------------------------------------------------------------

create view public.daily_escalation_stats
with (security_invoker = true)
as
select
    tenant_id,
    date_trunc('day', created_at) as day,
    reason,
    channel,
    count(*) as escalations
from public.escalations
group by tenant_id, date_trunc('day', created_at), reason, channel;

grant select on public.daily_escalation_stats to app_backend;

-- ---------------------------------------------------------------------------
-- tenant_overview — one row per tenant, 30-day totals + last-activity
-- timestamps. The operator landing page's per-tenant query: called once per
-- tenant_id in get_repository().list_ids(), each time with that tenant's own
-- JWT, so `public.tenants`' own RLS (0002_rls.sql) already scopes the base
-- row to exactly one tenant per call.
-- ---------------------------------------------------------------------------

create view public.tenant_overview
with (security_invoker = true)
as
select
    t.tenant_id,
    coalesce(c.calls_30d, 0) as calls_30d,
    coalesce(c.call_seconds_30d, 0) as call_seconds_30d,
    coalesce(c.cost_usd_30d, 0) as cost_usd_30d,
    c.last_call_at,
    coalesce(j.jobs_30d, 0) as jobs_30d,
    j.last_job_at,
    coalesce(e.escalations_30d, 0) as escalations_30d,
    e.last_escalation_at,
    coalesce(cs.chat_sessions_30d, 0) as chat_sessions_30d,
    cs.last_chat_at
from public.tenants t
left join (
    select tenant_id, count(*) as calls_30d, sum(duration_seconds) as call_seconds_30d,
           sum(cost_usd) as cost_usd_30d, max(created_at) as last_call_at
    from public.calls
    where created_at >= now() - interval '30 days'
    group by tenant_id
) c on c.tenant_id = t.tenant_id
left join (
    -- "Bookings", not "booking attempts": a cancelled job is excluded, the
    -- same reasoning the tenant_metrics() RPC below applies. daily_job_stats
    -- above keeps every status (a cancelled/completed breakdown is still
    -- useful there); this headline number specifically means "still stands".
    select tenant_id, count(*) as jobs_30d, max(created_at) as last_job_at
    from public.jobs
    where created_at >= now() - interval '30 days'
      and status != 'cancelled'
    group by tenant_id
) j on j.tenant_id = t.tenant_id
left join (
    select tenant_id, count(*) as escalations_30d, max(created_at) as last_escalation_at
    from public.escalations
    where created_at >= now() - interval '30 days'
    group by tenant_id
) e on e.tenant_id = t.tenant_id
left join (
    select tenant_id, count(*) as chat_sessions_30d, max(started_at) as last_chat_at
    from public.chat_sessions
    where started_at >= now() - interval '30 days'
    group by tenant_id
) cs on cs.tenant_id = t.tenant_id;

grant select on public.tenant_overview to app_backend;

-- ---------------------------------------------------------------------------
-- tenant_metrics(from_day, to_day) — a caller-windowed bundle in one round
-- trip, for the admin dashboard's 7/30/90-day picker. Deliberately:
--   * `language sql stable`, NOT `security definer` — it runs as the
--     invoking role, so RLS applies exactly as if the caller had run the
--     query directly. The identical rule 0003_vault.sql::get_tenant_secret
--     established for its own RPC: derive the tenant from the caller's own
--     JWT-scoped access, never accept it as a parameter to trust.
--   * No tenant_id parameter at all — there is nothing to distrust, unlike
--     a naive design that accepted one and trusted the caller not to lie.
-- ---------------------------------------------------------------------------

create or replace function public.tenant_metrics(from_day date, to_day date)
returns table (
    tenant_id text,
    calls bigint,
    call_seconds double precision,
    cost_usd double precision,
    jobs bigint,
    escalations bigint,
    chat_sessions bigint,
    chat_messages bigint
)
language sql
stable
as $$
    select
        t.tenant_id,
        coalesce(c.calls, 0),
        coalesce(c.call_seconds, 0),
        coalesce(c.cost_usd, 0),
        coalesce(j.jobs, 0),
        coalesce(e.escalations, 0),
        coalesce(cs.chat_sessions, 0),
        coalesce(cm.chat_messages, 0)
    from public.tenants t
    left join (
        select tenant_id, count(*) as calls, sum(duration_seconds) as call_seconds,
               sum(cost_usd) as cost_usd
        from public.calls
        where created_at::date between from_day and to_day
        group by tenant_id
    ) c on c.tenant_id = t.tenant_id
    left join (
        -- "Bookings", not "booking attempts" — a cancelled job doesn't
        -- count. Same distinction as tenant_overview.jobs_30d above.
        select tenant_id, count(*) as jobs
        from public.jobs
        where created_at::date between from_day and to_day
          and status != 'cancelled'
        group by tenant_id
    ) j on j.tenant_id = t.tenant_id
    left join (
        select tenant_id, count(*) as escalations
        from public.escalations
        where created_at::date between from_day and to_day
        group by tenant_id
    ) e on e.tenant_id = t.tenant_id
    left join (
        select tenant_id, count(*) as chat_sessions
        from public.chat_sessions
        where started_at::date between from_day and to_day
        group by tenant_id
    ) cs on cs.tenant_id = t.tenant_id
    left join (
        select tenant_id, count(*) as chat_messages
        from public.chat_messages
        where created_at::date between from_day and to_day
        group by tenant_id
    ) cm on cm.tenant_id = t.tenant_id
$$;

-- Functions grant EXECUTE to PUBLIC by default (unlike views/tables) —
-- revoke it explicitly, same as 0003_vault.sql's RPCs.
revoke all on function public.tenant_metrics(date, date) from public;
grant execute on function public.tenant_metrics(date, date) to app_backend;

-- ---------------------------------------------------------------------------
-- Indexes — every view above groups by (tenant_id, created_at) or
-- (tenant_id, started_at). Without these this is a slow page in six months
-- nobody attributes to Phase 8; `calls` today only has
-- `unique (tenant_id, provider_call_id)`, and `escalations`/`messages` have
-- none at all. chat_messages already has a composite index from 0006_chat.sql
-- (tenant_id, session_id, created_at), but session_id sits between tenant_id
-- and created_at in that index, so it doesn't help a pure per-day scan across
-- every session — it gets its own index here too, for the same reason
-- daily_chat_stats groups it by day.
-- ---------------------------------------------------------------------------

create index if not exists calls_tenant_created_idx on public.calls (tenant_id, created_at);
create index if not exists escalations_tenant_created_idx
    on public.escalations (tenant_id, created_at);
create index if not exists messages_tenant_created_idx on public.messages (tenant_id, created_at);
create index if not exists chat_sessions_tenant_started_idx
    on public.chat_sessions (tenant_id, started_at);
create index if not exists chat_messages_tenant_created_idx
    on public.chat_messages (tenant_id, created_at);
