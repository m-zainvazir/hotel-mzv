# Phase 9 — Cal.com over MCP · Bot lifecycle · Per-bot knowledge (RAG)

## Context

Phases 1–8 are done and live-verified. The brain answers voice and chat for two
tenants, books a real Cal.com calendar, persists behind real RLS with per-tenant
Vault secrets and a durable checkpointer, loads per-tenant MCP tools, runs on
Railway in `us-east4`, and has an operator admin panel with analytics and a
working config editor.

Three gaps stop it being a platform you can grow rather than a deployment you
maintain. All three are confirmed in the code, not anticipated:

1. **Cal.com is hardwired.** `app/tools/booking/calcom.py` speaks Cal.com's REST
   API directly — `GET /slots`, `POST /bookings`, two pinned `cal-api-version`
   headers. The calendar should be reached through an external MCP server
   instead, *behaving exactly as it does today*.
2. **A bot can only be created from a shell, and can never be removed.**
   `app/channels/admin.py` has exactly one write route — `PUT
   /admin/api/tenants/{tid}` — and it 404s unless the tenant already exists
   (`admin.py:186` calls `get_tenant_config` first). There is no POST-create and
   no delete of any kind, in any layer. `scripts/onboard_tenant.py` is the only
   create path and it needs a dev box. `app/tenancy/sync.py` has no
   delete-the-tenant-row primitive — only `_delete_absent_services` /
   `_delete_absent_mcp_servers` for children.
3. **No bot knows anything outside its prompt.** There is no retrieval layer:
   zero occurrences of embeddings, vectors, pgvector, or any vector store
   anywhere in the repo. A tenant's knowledge is whatever fits in
   `content/system-prompt.md`'s placeholders plus `system_prompt_override`.

**Outcome:** Cal.com reached only through MCP with byte-identical conversational
behaviour; an admin panel where an operator creates, clones, archives and purges
as many bots as they like; and a per-bot knowledge base every bot — including
every future one — gets automatically.

**A note on "can I run many different bots?"** — the architecture already does
this and has since Phase 4. A tenant *is* a bot: `TenantConfig` carries its own
`system_prompt_override`, `persona`, `greeting`, `trade`, services, hours,
timezone, voice, Vapi assistant, booking provider, emergency policy and MCP
servers, isolated by `tenant_id` with `FORCE ROW LEVEL SECURITY` underneath. A
hotel, a dental clinic and an electrician already run side by side on one
deployment with different prompts and different tools. Phase 9 does not build
multi-bot — it builds **lifecycle management** for the bots the platform can
already run.

## Decisions locked

| | |
|---|---|
| **Cal.com MCP server** | **Official hosted `https://mcp.cal.com/mcp`** — streamable HTTP, OAuth 2.1. Gated on a Step 0 spike; first-party in-repo fallback documented below. |
| **MCP depth** | **Provider layer, not tool tier.** A new `McpBookingProvider` behind the unchanged `BookingProvider` ABC. **No tier-1 tool becomes model-facing MCP** — convention #2 and `content/README.md`'s "no native tool ever moves to MCP" both hold. |
| **Bot removal** | **Archive by default; purge as a separate, explicitly-confirmed, operator-only action.** |
| **Vector store** | **pgvector in Supabase**, behind a new `KnowledgeStore` protocol so a later swap to a dedicated vector DB is one implementation class. |
| **Embeddings** | **Gemini `gemini-embedding-001` at 768 dimensions**, raw httpx over `shared_async_client`. Zero new packages, matching the "raw httpx, no SDKs" precedent. |
| **Retrieval** | A **`search_knowledge` native tool the model calls** — not always-on prompt injection. |
| **Ingestion** | **Three sources: paste text · multi-file upload (`.pdf .docx .md .txt .csv`) · URL fetch/crawl.** Auto-deriving from tenant config was considered and **deliberately dropped** — see "Why config auto-indexing is out". |

### Why the swap is at the provider layer, not the tool tier

"Perform exactly the way it is now" is a hard requirement, and moving
`check_availability` / `book_job` to model-facing MCP tools breaks four things
that are not obvious until you trace them:

- **The widget's quick-reply chips die.** `check_availability` is a
  `content_and_artifact` tool returning `(text, {"kind": "slots", ...})`;
  `app/brain/runner.py::_suggestions_artifact` turns that into a
  `BrainEvent("suggestions")` the widget renders as slot chips. MCP tools loaded
  through `langchain_mcp_adapters` set `ToolMessage.artifact` only for non-text
  content blocks — never a dict carrying `kind`. The chips would silently stop.
- **Acknowledgements degrade to generic filler.** `is_slow_tool`
  (`app/tools/registry.py:56`) does return `True` for a prefixed MCP name, so an
  acknowledgement still fires — but `acknowledgement_for` looks up
  `"{tool}.{channel}"` → `"{tool}"` → `"default"`, and a name like
  `calcom_create_booking` matches no bucket in
  `content/acknowledgements.json`. Every booking turn would say "One moment."
  instead of the booking-specific line.
- **`send_confirmation` breaks.** It looks the job up by `job_id` in the local
  store (`app/tools/messaging_tools.py:21-48`). Cal.com's `uid` is not that id —
  `calcom.py:281-283` is explicit that the local row stays authoritative.
- **The `ERROR:` string discipline is lost.** `booking_tools.py` maps every
  provider failure to a guarded string containing "Do NOT say it is booked".
  Raw MCP tool output goes straight to the model, which will cheerfully confirm
  a booking that never happened.

At the provider layer none of this applies: the model's tool surface is
unchanged, and Cal.com is still reached only through MCP.

### Why config auto-indexing is out

Indexing `TenantConfig` into the knowledge base was considered and rejected.
`content/system-prompt.md` already carries `${services}` and `${business_hours}`,
substituted by `app/brain/prompts/system.py` on **every turn**, so the model
already has that data without a tool call. Indexing it would create a second
source of truth that can silently drift from the live prompt — and this
codebase's deliberate posture everywhere (transcript writes, MCP loads, the
tenant snapshot) is that storage failures degrade to a log line rather than
breaking a conversation, so a failed re-index would be invisible while producing
confidently wrong answers. It would also spend `top_k` slots on content the model
already holds, degrading retrieval for the question actually asked.

The case where it pays is a bot whose catalogue is too large for its prompt — and
the right move there is to **trim the prompt and let RAG carry the detail**
(replacing, not duplicating). That's a separate, larger change; it goes to
`plans/phase10.md`.

## What I need from you

**Nothing blocks Steps 0–9.** Three things, in order of when they bite:

1. **Turn the admin panel on in production** (Step 0b) — `ADMIN_ENABLED=true`,
   `ADMIN_AUTH_TOKEN` (32+ chars), `TENANT_SOURCE=supabase`, set **together in a
   single Railway variable update**. `app/preflight.py` hard-fails on
   `admin_enabled` + `tenant_source="json"` (the phantom edit), so setting them
   one at a time crashes the service in between. This is Phase 8's one
   outstanding item and Part B is inert without it.
2. **A Cal.com account you can authorize interactively** (Step A2) — the hosted
   MCP server's OAuth flow opens a browser once per tenant. `hotel-mzv`'s
   existing account is fine.
3. **Nothing for RAG.** `GOOGLE_API_KEY` is already set and Gemini is already
   the active LLM provider, so embeddings need no new account.

---

## Implementation

Each step ends with a green `pytest` and `ruff check .`. Parts are independent;
suggested order is **B → C → A**, and each can land as its own commit.

### Step 0 — baseline

`pytest` → record the count. `ruff check .`, `ruff format --check .`. Confirm
`widget/dist/.buildhash` and `admin/dist/.buildhash` both match — **both guards
skip silently when `dist/` is absent**, so a green suite is not proof either
bundle is built.

### Step 0b — the production admin switch

Per "What I need from you" item 1. Verify afterwards: `/admin` loads,
`/admin/api/session` returns `kind: "operator"`, `/health` reports
`tenant_source: "supabase"` with `problems: []`.

---

## Part B — bot lifecycle

### Step B1 — `tenant_id` validation and the `archived` status

`app/tenancy/models.py`:

- **`tenant_id` has no validator today.** Add `^[a-z0-9][a-z0-9-]{1,47}$`,
  mirroring `McpServerConfig._legal_tool_prefix`'s reasoning
  (`models.py:208-222`). It becomes a checkpointer thread-id prefix
  (`f"{tenant}:vapi:{call.id}"`), a Vault secret prefix (`<tenant_id>::<key>`),
  and a filename in `content/tenants/`. Unvalidated it is a real hazard, and the
  admin create route is the first thing that would ever accept a hostile one.
- `status` Literal gains `"archived"` (`active | paused | onboarding |
  archived`). Update `0001_schema.sql`'s `CHECK` via the new migration.

`app/tenancy/loader.py::resolve_tenant_id` and the channel entry points refuse an
archived tenant with a clean, logged error rather than answering. **Test both
channels** — an archived bot must not answer a Vapi call or a widget handshake.

### Step B2 — `0010_lifecycle.sql`

- `alter table public.tenants drop constraint ... / add constraint` for the new
  status value.
- **`grant delete on public.<table> to app_backend`** for every table. Today
  `0002_rls.sql` grants only `select, insert, update` — no table anywhere grants
  delete, so purge has no path. (Alternative: purge runs on the secret key as an
  operator action, matching `sync.py`'s posture. Decide once, in the migration
  header, and don't leave both.)
- Header note: creates no table, so `tests/test_migrations.py` needs nothing —
  same shape as `0007_mcp.sql` / `0009_admin.sql`.

### Step B3 — the write layer (`app/tenancy/admin.py`)

Three new functions beside the existing `save_tenant`:

```python
async def create_tenant(config, *, client=None) -> TenantConfig
async def set_tenant_status(tenant_id, status, *, client=None) -> TenantConfig
async def purge_tenant(tenant_id, *, client=None) -> dict[str, int]
```

- `create_tenant` refuses an existing `tenant_id` (409), writes through the
  existing `sync_tenant()` (reusing its `_admin_client` with the load-bearing
  `Prefer: resolution=merge-duplicates,return=representation` header — the
  duplicate-builder bug Phase 8 already fixed once), then runs the same
  invalidation dance `save_tenant` does at `admin.py:210-218`:
  `clear_tenant_cache()` + duck-typed `repository.refresh()` / `.invalidate()`.
  Without it the new bot doesn't appear until the 300s background refresh.
- **`purge_tenant` deletes in explicit FK order**, because most FKs have no
  cascade: `services`, `mcp_servers` and `voice_consents` are `on delete
  cascade`, but `jobs`, `calls`, `messages`, `escalations`, `chat_sessions` and
  `chat_messages` are plain references — Postgres rejects a naive
  `DELETE /tenants` for any bot that has ever taken a call. Order:

  ```
  knowledge_chunks → knowledge_documents → chat_messages → chat_sessions
  → escalations → messages → jobs → calls → services → mcp_servers
  → voice_consents → tenants
  ```

  Then: delete that tenant's Vault secrets, delete the Vapi assistant if
  `vapi.assistant_id` is set, and remove `content/tenants/<id>.json` if present.
  Returns per-table row counts and logs them — a purge must be auditable.
- Preconditions on purge: the tenant must already be `archived`, and the caller
  must supply the `tenant_id` again in the body as a typed confirmation.

### Step B4 — the routes (`app/channels/admin.py`)

```
POST /admin/api/tenants                    create (blank | template | clone)
POST /admin/api/tenants/{tid}/archive
POST /admin/api/tenants/{tid}/restore
POST /admin/api/tenants/{tid}/purge
```

`POST`, not `DELETE`/`PATCH`: `app/main.py`'s CORS `allow_methods` is
`["GET","POST","OPTIONS"]` and `admin.py`'s module docstring (L3-8) records why
widening it on a wildcard origin isn't worth doing for a same-origin panel.

- Create and purge depend on `require_admin` and assert
  `principal.kind == "operator"` — so they behave correctly the day tenant login
  lands (`plans/phase10.md` item 14) with no re-audit.
- Archive/restore depend on `require_tenant_access`, tenant id from the **path**,
  never the body — the invariant `put_tenant` already enforces at `admin.py:190`.
- Validation is **Pydantic, entire**: `TenantConfig.model_validate` →
  `ValidationError.errors()` → 422 with `loc` paths, reusing
  `_validation_errors()` (`admin.py:156`). No hand-written rules.
- Three create modes: **blank**, **template**
  (`content/templates/*.json` — new: hotel, clinic, trades, salon, restaurant,
  seeded from the two existing tenants' shapes), or **clone** an existing bot
  with identity fields cleared (`tenant_id`, `phone_numbers`, `widget_keys`,
  `vapi.assistant_id`, `voice.voice_id`, `booking.event_type_id`).
- Created as `status: "onboarding"` with a generated `widget_key`, then flipped
  to `"active"` — matching `onboard_tenant.py:117,191`'s ordering.

**Supabase is the source of truth for panel-created bots.** Railway's filesystem
is ephemeral, so writing `content/tenants/*.json` from the container is
misleading rather than helpful; `python -m scripts.sync_tenants --export`
(already built) pulls live config down when you want it committed as a
seed/fallback. Note the consequence honestly: a panel-created bot has **no JSON
fallback**, so if its `config` blob ever fails validation
`SupabaseTenantRepository` has nothing to fall back to
(`supabase_repository.py:136-143`). `--export` after creating a bot is the
mitigation, and it goes in the docs.

### Step B5 — admin UI (`admin/src/`)

- `Sidebar` (`App.tsx:114`) gains **"+ New bot"**; archived bots render in a
  collapsed group.
- `router.ts` gains `{name:"new-tenant"}`. **Guard required:** `parse()` (L13)
  currently matches `#/tenants/new` as `tenantId="new"`, so the explicit check
  must come first.
- New `views/NewTenant.tsx` — mode picker (blank/template/clone) plus the minimal
  field set (`tenant_id`, `name`, `trade`, `greeting`,
  `emergency.escalation_phone` — everything else defaults), reusing `Config.tsx`'s
  existing section components rather than a second form implementation.
- A **Danger Zone** at the bottom of `Config.tsx`: Archive / Restore / Purge,
  purge behind a typed-`tenant_id` confirmation and disabled unless archived.
- **Rebuild and commit `admin/dist`** or `tests/test_admin_bundle.py` fails. On
  this box see `admin/README.md`'s rollup pin.

### Step B6 — the hard-delete runbook

`infra/README.md` and `content/README.md` get an explicit **"removing a bot"**
section: what archive does, what purge does, the exact FK order, the manual SQL
fallback if the panel is unavailable, and the reminder that purge also destroys
every call transcript for that tenant.

---

## Part C — per-bot knowledge (RAG)

### Step C1 — config and models

`app/config.py` — new **real `Settings` fields** (never ad-hoc `os.environ`;
`hermetic_settings` strips only names matching a field):
`knowledge_enabled: bool = False`, `embedding_provider: Literal["google",
"openai"] = "google"` (mirroring `llm_provider`), `embedding_model =
"gemini-embedding-001"`, `embedding_dimensions: int = 768`,
`embedding_timeout_seconds: float = 20.0`, `knowledge_max_upload_bytes`,
`knowledge_source: Literal["supabase"] = "supabase"` (the swap seam).

`app/tenancy/models.py` — a frozen `KnowledgeSettings` on `TenantConfig`:
`enabled: bool = False`, `top_k: int = Field(4, gt=0, le=20)`,
`min_similarity: float = Field(0.35, ge=0.0, le=1.0)`,
`max_chunks: int = 5000` (a per-tenant quota against the free-tier ceiling).

`GET /health` gains `"knowledge"`: `off` / `ready` / `unavailable`, following the
existing `store` / `widget` / `mcp` / `admin` fields.

### Step C2 — `0011_knowledge.sql`

```sql
create extension if not exists vector;

create table public.knowledge_documents (
    id text primary key,
    tenant_id text not null references public.tenants (tenant_id) on delete cascade,
    title text not null default '',
    source_type text not null default 'text'
        check (source_type in ('text', 'file', 'url')),
    source_ref text not null default '',
    status text not null default 'pending'
        check (status in ('pending', 'indexing', 'ready', 'failed')),
    error text, chunk_count integer not null default 0,
    bytes bigint not null default 0,
    created_at timestamptz not null default now(),
    indexed_at timestamptz
);

create table public.knowledge_chunks (
    id text primary key,
    tenant_id text not null references public.tenants (tenant_id) on delete cascade,
    document_id text not null references public.knowledge_documents (id) on delete cascade,
    ordinal integer not null default 0,
    content text not null default '',
    token_count integer not null default 0,
    embedding vector(768),
    created_at timestamptz not null default now()
);
```

Conventions carried over verbatim so `tests/test_migrations.py` passes unchanged:
`text` primary keys, `not null default ''` on non-optional strings, `status` as
`text` + `CHECK` not a PG enum, `enable` **and** `force row level security`, a
`tenant_isolation` policy reading `auth.jwt() ->> 'tenant_id'`, and an explicit
grant to `app_backend`.

Three things that are **not** boilerplate:

- **HNSW index** on `embedding` with `vector_cosine_ops`, plus
  `(tenant_id, document_id)` btree.
- **`revoke all on public.knowledge_documents, public.knowledge_chunks from anon,
  authenticated;` explicitly.** CLAUDE.md records that this project has
  `ALTER DEFAULT PRIVILEGES ... GRANT ALL ON TABLES TO anon, authenticated,
  service_role`, so every newly created object silently inherits it — and
  `revoke ... from public` does **not** touch grants held by named roles. Skip
  this and every tenant's uploaded documents are readable with the anon key,
  defended by RLS alone.
- **`match_knowledge_chunks(query_embedding vector, match_count int,
  min_similarity float)`** — `language sql stable`, **`security invoker`, never
  `security definer`, and no `tenant_id` parameter**. The tenant comes from the
  caller's own JWT claim, the identical rule `0003_vault.sql::get_tenant_secret`
  and `0008_analytics.sql::tenant_metrics` already establish. `revoke all from
  public`, `grant execute to app_backend`.

Plus a `pg_cron` sweep for orphaned chunks, following `0004_retention.sql`.

### Step C3 — the storage layer

`app/db/store.py` — a new `KnowledgeStore` Protocol following the house
convention exactly (`tenant_id` first, keyword-only options, sync + `a`-prefixed
async twins): `add_document`, `list_documents`, `get_document`,
`delete_document`, `set_document_status`, `upsert_chunks`, `search_chunks`.

New models in `app/db/models.py`: `KnowledgeDocument`, `KnowledgeChunk`,
`KnowledgeHit` (chunk + similarity + document title).

- **`InMemoryStore`** implements it over its own dicts with **pure-Python cosine
  similarity** — `numpy` is not in the dependency tree and would add ~20MB to the
  image. This is what keeps the whole suite offline. Add the new dicts to
  `reset()` or tests leak state.
- **`SupabaseStore`** implements it over PostgREST: writes carry
  `Prefer: return=representation`, every query carries `tenant_id=eq.` explicitly
  even though RLS enforces it (convention #3), and `search_chunks` posts to
  `/rpc/match_knowledge_chunks` through the existing `_request` helper — the same
  path `atenant_metrics` uses at `supabase_store.py:376-386`, carrying the
  **tenant JWT, never the secret key**.

**This protocol is the swap seam.** Moving to Qdrant/Pinecone later means writing
one new `KnowledgeStore` implementation and flipping `knowledge_source`; the
tool, the ingestion pipeline, the admin UI and every test are untouched — the
same shape `BookingProvider` already proved when Cal.com replaced Google Calendar
as a new file rather than a rewrite.

### Step C4 — `app/rag/` (new package)

- **`embeddings.py`** — `POST {google_api_base}/models/{model}:embedContent` with
  `outputDimensionality: 768`, over `shared_async_client` keyed
  `f"embeddings:{base_url}:{sha256(api_key)[:12]}"` — the key is baked into the
  client's headers here (unlike the Supabase case where a rotating JWT rides
  per-request), so it **must** be in the cache key. Batched, one retry on 429,
  `EmbeddingError` on failure. Dispatch on `embedding_provider` so OpenAI is a
  later config flip, not a refactor.
- **`chunking.py`** — recursive splitting on paragraph → sentence → word
  boundaries, ~800 tokens with ~15% overlap. Stdlib only; no
  `langchain-text-splitters`.
- **`extract.py`** — `.md`/`.txt`/`.csv` free; `.pdf` via `pypdf`; `.docx` via
  `python-docx`; HTML via a stdlib `html.parser.HTMLParser` subclass (no
  `beautifulsoup4`). **New `rag` optional extra** — it must be added to
  `pyproject.toml`, to `infra/Dockerfile`'s `deps`-stage extras list, **and**
  regenerated into `infra/requirements.lock.txt`; miss any one and the runtime
  image silently lacks it. Extend `tests/test_deploy_config.py` to assert the
  pairing, which is exactly the class of gap that lint exists for.
- **`ingest.py`** — extract → chunk → embed → upsert, run as a **background
  task** with per-document status transitions, so a 200-page PDF can't hang the
  request. Enforces `knowledge.max_chunks` and `knowledge_max_upload_bytes`.
- **`crawl.py`** — single page or same-domain crawl with a depth and page cap,
  over `shared_async_client`. Refuses private/loopback addresses (SSRF — the same
  concern `plans/phase10.md` item 12 raises for tenant-submitted MCP URLs).

### Step C5 — the tool

`app/tools/knowledge_tools.py` — `search_knowledge(query: str, config)`, reading
`tenant_id` from `RunnableConfig` like every other tool, never from a model
argument.

- Added to `SLOW_TOOLS` (`app/tools/registry.py:31`) — an embedding round trip
  plus a vector query is dead air on voice — with its own entry in
  `content/acknowledgements.json`.
- **Bound conditionally through `native_tools_for(tenant, channel)`**, only when
  `tenant.knowledge.enabled`. That function has been a deliberate no-op since
  Phase 1 (`registry.py:42`: `del tenant, channel  # every tenant gets the full
  critical path`); this is the first real use of the seam it was written for, and
  it keeps the ~1,460-token fixed floor unchanged for bots with no knowledge base.
  **Both bind sites must agree** — `app/brain/nodes/reason.py:44` and
  `app/brain/nodes/tools.py:32` each call it independently, and a mismatch is
  exactly the class of bug Phase 6's dynamic `ToolNode` fixed.
- Returns formatted excerpts with source titles. An empty result returns a plain
  "nothing on file about that" string, never an error.
- `content/system-prompt.md` gains a `${knowledge_rule}` placeholder (empty for
  bots without knowledge): prefer retrieved content over invention, and say so
  when it isn't there.

### Step C6 — admin API + UI

Routes on the existing admin router, all `require_tenant_access`:

```
GET    /admin/api/tenants/{tid}/knowledge              documents + status
POST   /admin/api/tenants/{tid}/knowledge/text         paste
POST   /admin/api/tenants/{tid}/knowledge/upload       multipart, multiple files
POST   /admin/api/tenants/{tid}/knowledge/url          fetch or crawl
POST   /admin/api/tenants/{tid}/knowledge/{doc}/reindex
POST   /admin/api/tenants/{tid}/knowledge/{doc}/delete
POST   /admin/api/tenants/{tid}/knowledge/search       preview
```

A **Knowledge** tab in `TenantView.tsx` carrying the three sources: a titled
paste box; **multi-select drag-and-drop upload** (`.pdf .docx .md .txt .csv`,
several at once, each queued independently with its own status); and a URL field
with a crawl toggle. Plus a document list with per-file status and chunk count,
delete, re-index, and a **search preview** so you can see what the bot would
retrieve for a question before it goes live.

---

## Part A — Cal.com through MCP

### Step A0 — the spike (a gate, not a formality)

Cal.com's hosted MCP server is `https://mcp.cal.com/mcp`, streamable HTTP, **OAuth
2.1 only** — the API-key path is the `npx @calcom/cal-mcp` **stdio** build, which
is unusable here on three counts: `MCP_ALLOW_STDIO` is off by design (a command
string is RCE on the box holding every tenant's secrets), stdio and the Postgres
checkpointer are mutually exclusive on this Windows box (Selector vs Proactor
event loop), and one `CAL_API_KEY` per process destroys the per-tenant Vault
design.

Before writing anything downstream, in a throwaway script, establish and record:

1. Does `/.well-known/oauth-protected-resource` → authorization-server metadata
   discovery work?
2. Is **Dynamic Client Registration** (RFC 7591) supported?
3. Are **refresh tokens** issued? (Everything headless depends on this.)
4. What scopes exist, and does one cover availability + booking?
5. **The exact request and response shapes of `get_availability` and
   `create_booking`.** Cal.com's docs name the tools but not their parameters —
   this is the single biggest unknown in Part A.

Same posture as Phase 6's Step 0 dependency probe: two minutes in a scratch
script versus a day discovering it downstream.

**If the spike fails on 2 or 3**, fall back to a **first-party in-repo MCP
server**: `scripts/calcom_mcp_server.py`, `FastMCP` streamable-HTTP, a sibling of
`scripts/demo_mcp_server.py`, wrapping the already-proven `CalcomBookingProvider`
and exposing exactly two tools, with the Cal.com key travelling per request so
per-tenant secrets keep working. The `McpBookingProvider` seam below is identical
either way, so the fallback costs a config change, not a rewrite — and Cal.com is
still reached only through MCP.

### Step A1 — OAuth (`app/mcp/oauth.py`, new)

Split into an interactive half that runs once and a headless half that runs
forever:

- **`scripts/authorize_calcom.py --tenant <id>`** (new) — discovery → DCR →
  authorization-code + PKCE, with a temporary `localhost` callback listener and a
  browser open. The operator signs into **that tenant's own Cal.com account**.
  This is better client onboarding than asking for an API key.
- The **refresh token and client credentials go into Supabase Vault**, scoped to
  that tenant, via the existing `set_tenant_secret` (`app/tenancy/secrets.py:134`)
  under `calcom_mcp_refresh_token` / `calcom_mcp_client_id` /
  `calcom_mcp_client_secret`.
- `app/mcp/oauth.py` does the headless refresh→access-token exchange with a TTL
  cache keyed per tenant, copying `app/db/auth.py::tenant_jwt`'s `RLock` +
  `dict[str, (ts, value)]` shape (a 60s cache against a longer expiry).
- **`app/mcp/connections.py`** gains a third auth shape beside the two it already
  documents: an `auth: "oauth"` server resolves a bearer at connect time instead
  of substituting `${secret}`. `redacted()` already blanks every header value
  unconditionally, so access tokens can never reach a log line.

**A revoked or expired grant must surface as `BookingError`, never an escaped
exception** — `booking_tools.py` turns `BookingError` into the recoverable
"calendar is not responding right now" string, whereas a bare exception produces
`FALLBACK_LINE` with the booking silently lost.

### Step A2 — `McpBookingProvider` (`app/tools/booking/mcp_calcom.py`, new)

Implements the unchanged `BookingProvider` ABC by opening an MCP session and
calling the server's tools. Reused verbatim from `calcom.py`, because these are
behaviour, not transport:

- `_event_type_for` — `service.event_type_id or booking.event_type_id`, with the
  `uses_shared_type` flag driving whether a duration is sent at all.
- `_placeholder_email` — `caller-<digits>@{booking_placeholder_email_domain}`,
  deterministic so a repeat caller is one Cal.com attendee. The default stays
  `example.com`; CLAUDE.md records that Cal.com validates deliverability and a
  made-up domain is a flat 400.
- `_metadata` carrying `job_id` as the reconciliation handle, within Cal.com's
  50-key / 40-char / 500-char limits.
- **Build the local `Job` first**, unpersisted, so `job.id` can go into metadata;
  take `start`/`end` from the provider's response rather than our arithmetic;
  store the returned uid as `calendar_event_id`. **The local row stays
  authoritative** — that's what `send_confirmation` looks up.
- The same error-mapping table: timeouts/transport → `BookingError`; conflict or
  "already booked"/"no longer available" → `SlotUnavailableError`; **raw provider
  text never leaks into the returned message**, only into logs.

`app/tenancy/models.py` — `BookingSettings.provider` Literal gains
`"mcp_calcom"`. `app/tools/providers.py::get_booking_provider` gains one lazy
branch matching the `calcom` one at `providers.py:28-31`.

**Latency.** `check_availability` sits on the 600–800ms budget (§13) and an MCP
handshake per call would blow it, so the provider holds a **per-tenant cached
session** keyed with a fingerprint over the resolved connection — the same
correctness-and-latency argument `app/mcp/client.py`'s cache already makes.

`app/tools/booking/calcom.py` is untouched and stays the fallback: reverting a
tenant is a one-word JSON edit.

### Step A3 — docs

`content/README.md` gains "Booking via MCP" — the `authorize_calcom` command, the
one-word provider flip, and the explicit statement that Cal.com still owns
availability either way.

---

### Step 9 — cross-cutting docs

`CLAUDE.md` (Phase 9 done; new gotchas: the `anon`/`authenticated` default-privilege
revoke, the FK-order purge and the missing `delete` grant, `native_tools_for`
finally doing something and needing both bind sites to agree, the OAuth
refresh-token-in-Vault design, panel-created bots having no JSON fallback);
`README.md` (status, a Knowledge section, the stubbed/real table);
`.env.example`; `admin/README.md`; `infra/README.md` (the removal runbook and the
new `rag` extra); and amendment notes on plan **§10** (booking transport) and
**§11** (MCP now carries a critical-path integration at the provider layer, which
§11's "long tail only" framing didn't anticipate).

---

## Testing

Everything offline on the existing `ScriptedChatModel` / `mock_http` /
`no_network` / `isolated_runtime` / `override_tenant` fixtures.

**New:**

- `tests/test_admin_tenant_crud.py` — create validates through Pydantic and 422s
  with `loc` paths; a duplicate id 409s; an illegal `tenant_id` slug 422s; clone
  clears every identity field; template create produces a valid config; archive
  makes `resolve_tenant_id` refuse the tenant on **both** channels; purge is
  refused unless archived and unless the typed confirmation matches; purge issues
  deletes in FK order (assert the request sequence); `clear_tenant_cache` +
  repository refresh ran after each mutation (spy).
- `tests/test_knowledge_store.py` — `InMemoryStore` cosine ranking and
  `min_similarity` cutoff; `SupabaseStore.search_chunks` posts to the **RPC**
  path carrying a **tenant JWT, not the secret key**; every request carries
  `tenant_id=eq.`; a zero-row search is `[]`, not an error.
- `tests/test_rag_chunking.py` — boundary preference, overlap, an oversized
  paragraph, empty input, unicode.
- `tests/test_rag_extract.py` — each extension; an unknown extension is a clean
  error; HTML stripping drops scripts and styles.
- `tests/test_knowledge_tool.py` — bound only when `knowledge.enabled`; **both
  bind sites agree**; an empty result is a string not an exception; `search_knowledge`
  is in `SLOW_TOOLS` and gets an acknowledgement; **bot A never retrieves bot B's
  chunks**.
- `tests/test_mcp_booking_provider.py` — tool-call argument shapes; the local
  `Job` is built before the call and keeps its id; response start/end override
  local arithmetic; the full error-mapping table; session caching within a turn;
  a dead server becomes `BookingError` not an exception.
- `tests/test_calcom_oauth.py` — PKCE challenge derivation; refresh exchange;
  token cache hit and expiry; a revoked grant degrades to `BookingError`; **a
  token never appears in a redacted connection**.

**Updated:** `tests/test_migrations.py` picks up `0010`/`0011` automatically;
`tests/test_deploy_config.py` gains the `rag`-extra pairing assertion;
`tests/test_api.py` for `/health`'s `knowledge` field; `tests/test_native_tools.py`
— `test_critical_path_tools_are_all_native` still holds (the five are unchanged;
`search_knowledge` is a sixth native tool, not a replacement).

### Live verification, in order

1. **Step A0's spike** — DCR, refresh tokens and both tool schemas recorded.
2. `authorize_calcom.py --tenant hotel-mzv` → refresh token in Vault; a
   **separate process** mints an access token headlessly.
3. Flip a scratch tenant to `"mcp_calcom"` → book through `chat_cli`, then
   **compare the Cal.com dashboard entry against a `"calcom"`-provider booking**:
   same event type, same duration, same attendee, same metadata.
4. `check_availability` p50 on `"calcom"` vs `"mcp_calcom"` against §13's budget.
   A visible delta means the session cache isn't being hit.
5. Widget: **slot chips still render** — the proof the artifact survived.
6. Create a **dental-clinic bot from the template in the panel** — different
   prompt, services, hours, trade — then hold a conversation with it through
   `chat_cli` and the widget. Confirm it never sees hotel-mzv's data.
7. Archive it → it stops answering on voice and chat. Restore → it answers.
   Purge a throwaway bot → every child row gone, no FK error, the tenant list
   updates without a restart.
8. Upload a multi-file batch (PDF + DOCX + MD) → all reach `ready`; ask a
   question only those documents can answer.
9. Crawl a URL and paste text; confirm both are retrievable and the search
   preview matches what the bot actually says.
10. **The isolation proof — Phase 9's equivalent of Phase 4's cross-tenant read
    check.** Mint a `northside-plumbing` tenant JWT by hand, query
    `/rest/v1/knowledge_chunks` and `/rpc/match_knowledge_chunks`: zero hotel-mzv
    rows. Repeat with the **anon** key: empty. This is the check that catches the
    default-privilege trap.
11. Railway redeploy: `/health` reports `knowledge: "ready"`, `problems: []`.

## Risks

1. **Cal.com's OAuth may not support DCR or refresh tokens.** Everything headless
   in Part A depends on both. Closed by Step A0 running first, with a fallback
   that costs a config change because the provider seam is transport-agnostic.
2. **Undocumented Cal.com MCP tool schemas.** The docs name `get_availability`
   and `create_booking` but not their parameters. Same mitigation; also the
   reason live check 3 compares two bookings side by side rather than just
   asserting one succeeded.
3. **An MCP hop on the critical path.** Provider-layer or not, booking now
   traverses an extra network leg. Bounded by the session cache and measured in
   live check 4 — this is a go/no-go, not a footnote.
4. **The default-privilege trap.** New tables inherit `GRANT ALL TO anon,
   authenticated` from the project's `ALTER DEFAULT PRIVILEGES`, and
   `revoke ... from public` does not undo it. Uploaded documents are the most
   sensitive thing this app has ever stored. Closed by the explicit revoke plus
   live check 10.
5. **Purge is irreversible and touches nine tables.** Mitigated by
   archive-first, the typed confirmation, operator-only access, row-count
   logging, and the offline test asserting delete order — but it remains the
   most destructive operation in the codebase.
6. **A panel-created bot has no JSON fallback.** If its `config` blob later fails
   validation, `SupabaseTenantRepository` has nothing to serve. `sync_tenants
   --export` after creation is the mitigation and belongs in the docs, not just
   here.
7. **Embedding cost and the free-tier ceiling.** ~5KB per chunk with its text
   against 500MB; `knowledge.max_chunks` is the per-tenant quota. Re-indexing a
   large corpus is a burst of embedding calls — batched and rate-limit-aware, but
   worth watching on the first real upload.
8. **Retrieved content enters the model's context verbatim** — the same
   prompt-injection surface `plans/phase6.md` Risk 5 names for MCP tool
   descriptions, now reachable by anyone who can upload a document. Blast radius
   is limited because native tools stay native (booking and escalation can't be
   reached this way), and uploads are operator-only until tenant login lands.
9. **A second no-op-until-now seam going live.** `native_tools_for` has ignored
   both arguments since Phase 1. Making it conditional means the bound tool set
   can now differ per tenant — and `reason` and the `tools` node call it
   independently. That's the exact shape of the Phase 6 bug; the test asserting
   both sites agree is the guard.

## Deferred

Re-crawl scheduling (manual re-index only); hybrid keyword+vector search and
reranking; per-tenant embedding cost metering (waits on the same usage-tier model
as `plans/phase10.md`'s rate limiting); **prompt-replacing config indexing** (the
"trim the prompt, let RAG carry the catalogue" variant — a real feature, larger
than what was dropped here); `cancel`/`reschedule` native tools now that the MCP
server exposes both (`plans/phase10.md` item 5); self-serve MCP registration
(item 12); and tenant login (item 14), which is what turns the operator-only bot
CRUD built here into something clients do for themselves.

## Est. effort

**A** 3–4 days, almost all of it OAuth and the spike. **B** 3–4 days, half of it
UI. **C** 5–6 days. That is comfortably three phases' worth of work in one plan —
splitting into 9a/9b/9c and shipping B first is entirely reasonable, and the
parts have no build-order dependency on each other.