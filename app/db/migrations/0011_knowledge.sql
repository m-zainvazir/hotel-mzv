-- Phase 9 Part C — per-bot knowledge base (RAG), pgvector-backed.
--
-- Same conventions as every prior migration: text primary keys, `not null
-- default ''` on non-optional strings, status as `text` + CHECK (not a PG
-- enum), `enable` AND `force row level security`, a `tenant_isolation`
-- policy reading `auth.jwt() ->> 'tenant_id'`, an explicit grant to
-- `app_backend`. Three things below are NOT boilerplate — see their own
-- comments: the HNSW index, the explicit revoke from anon/authenticated,
-- and match_knowledge_chunks' security-invoker/no-tenant-id-parameter shape.

create extension if not exists vector;

create table public.knowledge_documents (
    id           text primary key,
    tenant_id    text not null references public.tenants (tenant_id) on delete cascade,
    title        text not null default '',
    source_type  text not null default 'text'
                   check (source_type in ('text', 'file', 'url')),
    source_ref   text not null default '',
    status       text not null default 'pending'
                   check (status in ('pending', 'indexing', 'ready', 'failed')),
    error        text,
    chunk_count  integer not null default 0,
    bytes        bigint not null default 0,
    created_at   timestamptz not null default now(),
    indexed_at   timestamptz
);

create table public.knowledge_chunks (
    id           text primary key,
    tenant_id    text not null references public.tenants (tenant_id) on delete cascade,
    document_id  text not null references public.knowledge_documents (id) on delete cascade,
    ordinal      integer not null default 0,
    content      text not null default '',
    token_count  integer not null default 0,
    embedding    vector(768),
    created_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- RLS — same shape every table since 0002_rls.sql has used. Unlike every
-- other table in this schema, these two ALSO get a `delete` grant
-- (app_backend, not just select/insert/update): a tenant's own uploaded
-- document is reversible, tenant-owned content — "delete my own upload" is
-- ordinary tenant-scoped CRUD, not the cross-table destructive action
-- purge_tenant is (0010_lifecycle.sql deliberately did NOT add a delete
-- grant anywhere, and still doesn't need to — purge runs on the secret key).
-- RLS's tenant_isolation policy still means this delete grant can only ever
-- reach a tenant's own rows, never another tenant's.
-- ---------------------------------------------------------------------------

alter table public.knowledge_documents enable row level security;
alter table public.knowledge_documents force row level security;
drop policy if exists tenant_isolation on public.knowledge_documents;
create policy tenant_isolation on public.knowledge_documents
    for all
    using (tenant_id = (auth.jwt() ->> 'tenant_id'))
    with check (tenant_id = (auth.jwt() ->> 'tenant_id'));
grant select, insert, update, delete on public.knowledge_documents to app_backend;

alter table public.knowledge_chunks enable row level security;
alter table public.knowledge_chunks force row level security;
drop policy if exists tenant_isolation on public.knowledge_chunks;
create policy tenant_isolation on public.knowledge_chunks
    for all
    using (tenant_id = (auth.jwt() ->> 'tenant_id'))
    with check (tenant_id = (auth.jwt() ->> 'tenant_id'));
grant select, insert, update, delete on public.knowledge_chunks to app_backend;

-- CLAUDE.md's "Supabase's schema default privileges quietly re-grant
-- everything in public" gotcha: this project has `ALTER DEFAULT PRIVILEGES
-- ... GRANT ALL ON TABLES TO anon, authenticated, service_role`, so every
-- newly created table silently inherits full access for `anon` and
-- `authenticated` too — and `revoke ... from public` (used elsewhere in
-- this file) does NOT touch grants held by those named roles. Uploaded
-- documents are the most sensitive content this app stores yet; skip this
-- and they're readable with the anon key, defended by RLS alone.
revoke all on public.knowledge_documents, public.knowledge_chunks from anon, authenticated;

-- ---------------------------------------------------------------------------
-- Indexes. HNSW (not IVFFlat) for the embedding column: no training step
-- needed before it's useful, which matters at the small per-tenant corpus
-- sizes (`knowledge.max_chunks`, app/tenancy/models.py) this feature
-- targets — IVFFlat's accuracy depends on having enough rows to build
-- meaningful clusters, which a freshly-onboarded bot with a handful of
-- documents doesn't have yet.
-- ---------------------------------------------------------------------------

create index knowledge_chunks_embedding_idx on public.knowledge_chunks
    using hnsw (embedding vector_cosine_ops);
create index knowledge_chunks_tenant_document_idx
    on public.knowledge_chunks (tenant_id, document_id);
create index knowledge_documents_tenant_idx
    on public.knowledge_documents (tenant_id, created_at);

-- ---------------------------------------------------------------------------
-- match_knowledge_chunks — the vector search RPC `search_knowledge`
-- (app/tools/knowledge_tools.py) and SupabaseStore.search_chunks both call.
-- `language sql stable`, deliberately NOT `security definer` (Postgres
-- functions default to SECURITY INVOKER, same as tenant_metrics in
-- 0008_analytics.sql) — it runs as the calling role, so RLS on
-- knowledge_chunks/knowledge_documents applies exactly as if the caller ran
-- the query directly. NO tenant_id parameter: there's nothing to distrust,
-- the identical rule 0003_vault.sql::get_tenant_secret and
-- 0008_analytics.sql::tenant_metrics already establish — the tenant comes
-- from the caller's own JWT claim via RLS on both joined tables, not an
-- argument someone could lie about.
-- ---------------------------------------------------------------------------

create or replace function public.match_knowledge_chunks(
    query_embedding vector(768),
    match_count int default 4,
    min_similarity float default 0.35
)
returns table (
    chunk_id text,
    document_id text,
    document_title text,
    content text,
    similarity float
)
language sql
stable
as $$
    select
        c.id as chunk_id,
        c.document_id,
        d.title as document_title,
        c.content,
        1 - (c.embedding <=> query_embedding) as similarity
    from public.knowledge_chunks c
    join public.knowledge_documents d on d.id = c.document_id
    where c.embedding is not null
      and 1 - (c.embedding <=> query_embedding) >= min_similarity
    order by c.embedding <=> query_embedding
    limit match_count
$$;

revoke all on function public.match_knowledge_chunks(vector, int, float) from public;
grant execute on function public.match_knowledge_chunks(vector, int, float) to app_backend;

-- ---------------------------------------------------------------------------
-- Orphan sweep, following 0004_retention.sql's pg_cron pattern. Two
-- distinct kinds of "orphan" here:
--   1. A background ingestion task (app/rag/ingest.py) that crashed mid-run
--      leaves a document stuck in 'pending'/'indexing' forever, with no way
--      for the panel to offer a retry otherwise — swept to 'failed' after
--      an hour so "Re-index" becomes available again.
--   2. A defensive, redundant sweep for chunks whose document row is
--      somehow gone despite the FK's `on delete cascade` — belt and
--      suspenders, matching this codebase's existing posture of never
--      trusting a single mechanism alone (CLAUDE.md convention #3).
-- ---------------------------------------------------------------------------

select cron.schedule(
    'sweep-stalled-knowledge-documents',
    '30 * * * *',  -- hourly, offset from the checkpoint jobs in 0004
    $$
    update public.knowledge_documents
    set status = 'failed', error = 'ingestion timed out'
    where status in ('pending', 'indexing')
      and created_at < now() - interval '1 hour';
    $$
);

select cron.schedule(
    'sweep-orphaned-knowledge-chunks',
    '35 * * * *',
    $$
    delete from public.knowledge_chunks c
    where not exists (
        select 1 from public.knowledge_documents d where d.id = c.document_id
    );
    $$
);
