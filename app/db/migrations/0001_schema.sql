-- Phase 4 — core schema.
--
-- Text primary keys throughout: app-generated ids already look like
-- "job_<hex10>" (app/db/models.py), not UUIDs, and tenant_id / service slug
-- are natural keys — keeping `text` avoids an id-scheme migration.
--
-- Every non-optional `str` field on the corresponding Pydantic model gets
-- `not null default ''`: the model requires the app to always supply a real
-- value, so the default is a safety net only (a NULL round-tripping back
-- raises a mid-call ValidationError), never a legitimate empty state.
--
-- RLS is added in 0002_rls.sql, Vault access in 0003_vault.sql — this file
-- is schema only.

-- ---------------------------------------------------------------------------
-- tenants / services
--
-- The runtime tenant *read* path stays on content/tenants/*.json for Phase 4
-- (see plan "On the tenant read path"). These tables exist so
-- scripts/sync_tenants.py and scripts/onboard_tenant.py (Step 9) have
-- somewhere durable to write to ahead of that read-path flip. Everything
-- beyond the columns useful for lookups/joins (greeting, persona, hours,
-- emergency, booking, notifications, voice, vapi, mcp_servers) lives in the
-- `config` JSONB blob rather than as individual columns, since nothing reads
-- them relationally yet.
-- ---------------------------------------------------------------------------

create table public.tenants (
    tenant_id     text primary key,
    name          text not null default '',
    trade         text not null default '',
    timezone      text not null default 'UTC',
    status        text not null default 'active'
                    check (status in ('active', 'paused', 'onboarding')),
    phone_numbers text[] not null default '{}',
    widget_keys   text[] not null default '{}',
    config        jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now()
);

create table public.services (
    tenant_id        text not null references public.tenants (tenant_id) on delete cascade,
    slug             text not null,
    name             text not null default '',
    description      text not null default '',
    duration_minutes integer not null default 60
                       check (duration_minutes > 0 and duration_minutes <= 480),
    price_usd        numeric,
    emergency        boolean not null default false,
    aliases          text[] not null default '{}',
    -- Cal.com event-type override for this one service; falls back to
    -- tenants.config -> 'booking' -> 'event_type_id' when null.
    event_type_id    bigint,
    created_at       timestamptz not null default now(),
    primary key (tenant_id, slug)
);

-- ---------------------------------------------------------------------------
-- jobs — the authoritative booking row (plan §10). The calendar provider's
-- event id is a link (`calendar_event_id`), never the source of truth.
-- ---------------------------------------------------------------------------

create table public.jobs (
    id                 text primary key,
    tenant_id          text not null references public.tenants (tenant_id),
    customer_name      text not null default '',
    customer_phone     text not null default '',
    address            text not null default '',
    service_slug       text not null,
    service_name       text not null,
    scheduled_start    timestamptz not null,
    scheduled_end      timestamptz not null,
    status             text not null default 'scheduled'
                         check (status in ('scheduled', 'cancelled', 'completed')),
    channel            text not null default 'chat',
    calendar_event_id  text,
    notes              text,
    created_at         timestamptz not null default now()
);

-- Backs `scheduled_between` (conflict detection) — every call filters by
-- tenant + a status + time-window predicate.
create index jobs_tenant_schedule_idx
    on public.jobs (tenant_id, status, scheduled_start, scheduled_end);

-- ---------------------------------------------------------------------------
-- calls — one row per voice conversation, upserted by (tenant_id,
-- provider_call_id) since Vapi can resend an end-of-call report
-- (InMemoryStore.record_call mirrors this; see app/db/memory_store.py:81-95).
-- ---------------------------------------------------------------------------

create table public.calls (
    id                text primary key,
    tenant_id         text not null references public.tenants (tenant_id),
    provider_call_id  text not null default '',
    from_number       text,
    to_number         text,
    started_at        timestamptz,
    ended_at          timestamptz,
    duration_seconds  double precision,
    ended_reason      text,
    transcript        text,
    recording_url     text,
    cost_usd          double precision,
    channel           text not null default 'voice',
    created_at        timestamptz not null default now(),
    unique (tenant_id, provider_call_id)
);

-- ---------------------------------------------------------------------------
-- messages — OutboundMessage: SMS confirmations/reminders/alerts. "to" is
-- quoted because it is a reserved word in PostgreSQL.
-- ---------------------------------------------------------------------------

create table public.messages (
    id           text primary key,
    tenant_id    text not null references public.tenants (tenant_id),
    "to"         text not null default '',
    body         text not null default '',
    kind         text not null default 'confirmation'
                   check (kind in ('confirmation', 'reminder', 'alert')),
    provider     text not null default 'stub',
    provider_sid text,
    status       text,
    error        text,
    created_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- escalations — one row per emergency/handoff.
-- ---------------------------------------------------------------------------

create table public.escalations (
    id                text primary key,
    tenant_id         text not null references public.tenants (tenant_id),
    reason            text not null default '',
    transferred_to    text not null default '',
    caller_summary    text not null default '',
    callback_number   text,
    channel           text not null default 'chat',
    created_at        timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- mcp_servers — per-tenant registry (Phase 6). Composite key: a tenant names
-- its own servers, so (tenant_id, name) is the natural uniqueness rule.
-- ---------------------------------------------------------------------------

create table public.mcp_servers (
    tenant_id       text not null references public.tenants (tenant_id) on delete cascade,
    name            text not null,
    transport       text not null default 'http' check (transport in ('http', 'stdio')),
    url             text,
    command         text,
    args            text[] not null default '{}',
    -- A *reference* into Vault, never an inlined secret.
    auth_secret_ref text,
    created_at      timestamptz not null default now(),
    primary key (tenant_id, name)
);

-- ---------------------------------------------------------------------------
-- voice_consents — CLAUDE.md convention #6 has no exceptions: a tenant's
-- voice_id may only be set when a consent row exists for it (Step 8 adds the
-- enforcement once tenants.voice_id has an app-facing shape to check against).
-- ---------------------------------------------------------------------------

create table public.voice_consents (
    id                    bigint generated always as identity primary key,
    tenant_id             text not null references public.tenants (tenant_id) on delete cascade,
    voice_owner_name      text not null default '',
    consent_artifact_url  text not null default '',
    consent_artifact_hash text,
    sample_audio_hash     text,
    granted_by            text not null default '',
    consented_at          timestamptz not null default now(),
    created_at            timestamptz not null default now()
);
