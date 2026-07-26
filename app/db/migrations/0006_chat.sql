-- Phase 5 — durable chat transcripts.
--
-- NAMING TRAP: `public.messages` (0001_schema.sql) is OutboundMessage — SMS
-- confirmations/reminders/alerts — despite plan §6b listing it as "chat
-- transcripts". It never was; that's always been outbound SMS. These two new
-- tables are the actual chat transcripts: a voice call gets a `calls` row
-- (app/channels/webhooks.py), a chat conversation gets these.
--
-- A widget session starts at the `/chat/session` handshake
-- (app/channels/chat.py), before any message exists — chat_sessions.id is the
-- session_id minted there, not app-generated with the job_/call_/msg_ prefix
-- convention. chat_messages.session_id is a natural FK onto it.
--
-- Same `not null default ''`-on-every-non-optional-str rule as 0001_schema.sql:
-- the Pydantic models require a real value, so a NULL round-tripping back
-- would raise mid-call.

create table public.chat_sessions (
    id          text primary key,
    tenant_id   text not null references public.tenants (tenant_id),
    widget_key  text not null default '',
    origin      text,
    started_at  timestamptz not null default now()
);

create table public.chat_messages (
    id          text primary key,
    tenant_id   text not null references public.tenants (tenant_id),
    session_id  text not null references public.chat_sessions (id) on delete cascade,
    role        text not null default 'user' check (role in ('user', 'assistant')),
    content     text not null default '',
    created_at  timestamptz not null default now()
);

-- Backs "list this session's transcript in order" — every read filters by
-- tenant + session, same shape as jobs_tenant_schedule_idx in 0001_schema.sql.
create index chat_messages_session_idx
    on public.chat_messages (tenant_id, session_id, created_at);

-- ---------------------------------------------------------------------------
-- RLS — same shape as 0002_rls.sql: FORCE (not just ENABLE, so the table
-- owner can't bypass it), a tenant_id-scoped policy, and an explicit GRANT
-- (a perfect policy with no grant is a 403 that reads like an auth bug).
-- ---------------------------------------------------------------------------

alter table public.chat_sessions enable row level security;
alter table public.chat_sessions force row level security;
drop policy if exists tenant_isolation on public.chat_sessions;
create policy tenant_isolation on public.chat_sessions
    for all
    using (tenant_id = (auth.jwt() ->> 'tenant_id'))
    with check (tenant_id = (auth.jwt() ->> 'tenant_id'));
grant select, insert, update on public.chat_sessions to app_backend;

alter table public.chat_messages enable row level security;
alter table public.chat_messages force row level security;
drop policy if exists tenant_isolation on public.chat_messages;
create policy tenant_isolation on public.chat_messages
    for all
    using (tenant_id = (auth.jwt() ->> 'tenant_id'))
    with check (tenant_id = (auth.jwt() ->> 'tenant_id'));
grant select, insert, update on public.chat_messages to app_backend;

-- ---------------------------------------------------------------------------
-- Retention — chat transcripts carry guest names, phone numbers and whatever
-- else a caller volunteers, same PII concern 0004_retention.sql already
-- flagged for calls.transcript. 30 days here mirrors
-- CHAT_TRANSCRIPT_RETENTION_DAYS's default (app/config.py) — pg_cron runs
-- inside Postgres with no access to app Settings, so the two are two
-- separately-edited copies of the same number; keep them in sync by hand.
-- ---------------------------------------------------------------------------

select cron.schedule(
    'prune-chat-messages',
    '20 0 * * *',  -- daily, offset from the other daily job in 0004
    $$
    delete from public.chat_messages
    where created_at < now() - interval '30 days';
    $$
);

select cron.schedule(
    'prune-chat-sessions',
    '25 0 * * *',
    $$
    delete from public.chat_sessions
    where started_at < now() - interval '30 days'
      and not exists (
          select 1 from public.chat_messages m where m.session_id = chat_sessions.id
      );
    $$
);
