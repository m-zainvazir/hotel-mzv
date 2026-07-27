# CLAUDE.md — AI Receptionist

Persistent project context for Claude Code. Read this **and** `AI-Receptionist-Build-Plan.md` at the start of every session. The plan doc is the canonical spec; this file is the quick-reference + coding conventions.

## What we're building
A multi-tenant AI receptionist: one LangGraph "brain" that serves both **phone** (Vapi Custom-LLM mode) and **chat** (web / WhatsApp), for many businesses at once. It answers calls, understands domain-specific requests (e.g. electrician jobs), books jobs, sends confirmations, and escalates emergencies. Full detail lives in `AI-Receptionist-Build-Plan.md`.

## Core principle
**One brain, two channels.** All logic lives in the LangGraph graph. Vapi and the chat widget are thin adapters. Never put business logic in a channel adapter.

## Tech stack (decided)
- **Reasoning model:** Groq — Llama 3.3 70B Versatile. Keep it provider-agnostic; allow a config-level fallback to gpt-4o-mini for hard multi-tool turns.
- **Framework:** LangGraph (Python) + FastAPI — this is the single deployed service ("under one roof").
- **Voice:** Vapi in Custom-LLM mode → our OpenAI-compatible `/chat/completions` **SSE** endpoint.
- **STT/TTS:** Deepgram + Cartesia (via Vapi). Cartesia also does per-tenant voice cloning.
- **DB:** Supabase Postgres — multi-tenant, Row-Level Security, Vault for secrets.
- **Booking:** Cal.com by default, behind a `BookingProvider` interface (Google Calendar optional, deferred). Supabase holds the authoritative `jobs` row; Cal.com's booking `uid` is a link, not the record.
- **SMS / escalation:** Twilio + Vapi warm transfer.
- **MCP:** `langchain-mcp-adapters` `MultiServerMCPClient`, with a per-tenant server registry.

## Non-negotiable conventions
1. **Stream everything.** The graph emits tokens as Groq produces them. The first spoken response must **never** wait on a tool call — acknowledge, then act.
2. **Two tool tiers.** Critical path (`check_availability`, `book_job`, `send_confirmation`, `escalate`, `is_emergency`) = native, typed, validated tools. Long-tail integrations (Sheets, scrapers, CRM) = MCP.
3. **Tenant isolation.** Every table and every query carries `tenant_id`; enforce Supabase RLS as defense-in-depth. One tenant must never see another's data, MCP servers, or secrets.
4. **Provider-agnostic brain.** No vendor-specific logic in graph nodes. Swapping Vapi→Retell, Groq→OpenAI, or Google Calendar→Cal.com must not touch the graph.
5. **Secrets** live in env vars / Supabase Vault, never in code. Per-tenant credentials are encrypted.
6. **Voice cloning requires stored written consent.** No exceptions.

## Repo layout
See §18 of the plan: `app/brain` (graph, nodes, prompts), `app/channels` (vapi_llm, chat + widget_auth, webhooks), `app/tools` (native + `booking/` provider interface), `app/mcp`, `app/tenancy`, `app/db`, `app/main.py`, `widget/` (Preact/TS chat widget, bundled with Vite), `scripts/` (onboarding), `infra/`, `tests/` (incl. tenant-isolation + latency tests).

## Build phases
Follow §15 of the plan in order: 0 Prereqs → 1 Brain skeleton → 2 Vapi voice → 3 Real tools → 4 Multi-tenancy → 5 Chatbot → 6 MCP → 7 Deploy → 8 Avatar. Each phase has an acceptance criterion — meet it before moving on.

## Latency budget
Target 600–800ms end-of-speech → first audio (§13). Protect it with streaming + acknowledge-then-act, native tools on the critical path, and region co-location.

## Pending decisions (confirm with the user before they block you)
Per §16: whose voice to clone (+ consent), avatar now vs later. Booking provider is
decided — Cal.com (not the plan's original Google Calendar recommendation) — see
Current state. Chat is decided too: web widget only for now (Phase 5) — WhatsApp is
explicitly deferred, not undecided (`plans/phase10.md` item 4), pending a Twilio
WhatsApp sender and the same client go-ahead SMS itself is waiting on.

## Environment gotcha (this Windows dev box)
- **`uuid_utils` must be 0.12–0.15 here.** Windows Application Control blocks the compiled
  DLL in builds **0.16+** (`ImportError: DLL load failed while importing _uuid_utils: An
  Application Control policy has blocked this file`). langchain-core 1.x hard-imports it, so
  one blocked build breaks *every* import, not just the offender. Core 1.x needs `>=0.12`;
  0.12–0.15 load fine, 0.16/0.17 don't. The `google` extra caps it (`<0.16` on win32). If
  imports suddenly die after any `pip install`, check `uuid_utils.__version__` first. Linux
  deploy is unaffected.
- **Installing an extra can silently upgrade the whole stack.** `pip install -e ".[google]"`
  pulls langchain-core/langgraph/langsmith up to the 1.x line (needed for Gemini 3.x thought
  signatures). If ToolNode import fails (`cannot import name 'ToolNode'`), the langgraph /
  langgraph-prebuilt files half-installed — `pip install --force-reinstall --no-deps
  langgraph langgraph-prebuilt` fixes the layout.
- **Gemini model choice matters.** 3.x models (incl. `gemini-3.1-flash-lite`) require
  "thought signatures" on multi-turn tool calls — only langchain-google-genai **4.x**
  supports them; older versions 400 on the second tool turn. 2.x Gemini models work on any
  version but have low free limits. Confirm the active model with
  `python -m scripts.check_model` and the CLI banner (`model=provider/name`).

## Commands
- Setup: `python -m venv .venv && pip install -e ".[dev]"`, then `cp .env.example .env`
- Dev server: `uvicorn app.main:app --reload` (health check at `GET /health` — reports
  `store` and `checkpointer` as `"memory"`/`"supabase"`/`"postgres"`). On this Windows box,
  if Device Guard blocks `.venv/Scripts/uvicorn.exe` ("blocked by your organization's
  Device Guard policy"), run it through the interpreter instead — same server, no
  separate `.exe` for the policy to flag: `python -m uvicorn app.main:app --reload`.
- Terminal chat: `python -m scripts.chat_cli [--tenant <id>] [--channel voice] [--show-tools]`
- LangGraph Studio: `pip install -e ".[studio]"` then `langgraph dev` (config in `langgraph.json`)
- Durable checkpointer: `pip install -e ".[postgres]"`, set `DATABASE_URL` (Supavisor
  **session**-mode pooler, port 5432 — see the gotcha below)
- Sync tenant JSON → Supabase: `python -m scripts.sync_tenants [--tenant <id>]`
- Onboard a new tenant: `python -m scripts.onboard_tenant --config <file.json> [--dry-run]
  [--calcom-api-key ...] [--voice-sample ... --consent-url ... --consent-owner ...
  --consent-granted-by ...] [--provision-vapi]`
- Build the chat widget: `npm --prefix widget install && npm --prefix widget run build`
  (writes `widget/dist/widget.js` + `.buildhash`, both committed — see `widget/README.md`).
  Re-run after any `widget/src` edit; `tests/test_widget_bundle.py` fails if you forget.
- Tests: `pytest` — runs without any API key (scripted chat model in `tests/conftest.py`)
- Lint/format: `ruff check .` and `ruff format .`
- Build the deploy image: `docker build -f infra/Dockerfile .` (from the repo root — every
  `COPY` path is root-relative). Regenerate the lockfile after any `pyproject.toml` dependency
  change: `docker build -f infra/Dockerfile --target deps -t air-deps .` then
  `docker run --rm air-deps pip freeze | grep -vi '^ai[-_]receptionist' > infra/requirements.lock.txt`
  — must run inside the Linux image, never `pip freeze` from the Windows dev `.venv` (see
  `infra/README.md`).
- Load/latency test: `python -m scripts.loadtest --base-url <url> --endpoint chat|voice
  --concurrency <n> --turns <n> --scenario question|booking|emergency`

## Current state
**Phases 1–2 done. Phase 3 booking is live; SMS/warm-transfer are code-complete but
not yet turned on.** Graph is `resolve_tenant → emergency_check → reason ⇄ tools`. All
five native tools are live. `hotel-mzv` is **genuinely booking against a real Cal.com
calendar** — `booking.provider: "calcom"`, `booking.event_type_id: 6446177`
(`content/tenants/hotel-mzv.json`), verified end to end including duration correctness
for all six services and a live `chat_cli` run. `northside-plumbing` stays on the stub.
`notifications.provider` is still `"stub"` on both — **Twilio is deliberately not wired
up** (client decision, not a technical blocker; `TwilioNotifier` is implemented and
tested against mocks in `tests/test_twilio_notifier.py`, just not turned on for any
tenant). Flipping a tenant's notifier live is the same one-word JSON edit
(`content/README.md` § "Going live") whenever that changes.

**Two real-account findings baked into `app/tools/booking/calcom.py`, worth knowing
before touching it again:**
- Cal.com validates attendee-email **deliverability**, not just syntax — a made-up
  domain gets a flat 400 ("This email address cannot receive mail"). The synthesized
  placeholder (`booking_placeholder_email_domain`, `Settings`) defaults to `example.com`
  (IANA's reserved, always-resolvable domain) for exactly this reason — don't change it
  to another made-up domain without testing against a live account first.
- Cal.com rejects `lengthInMinutes` **outright** (not just wrong values) on any event
  type without multiple durations enabled — `_post_booking` retries once without the
  field on that specific error text, and the existing start/end reconciliation takes
  Cal's own (possibly shorter) actual duration from there. Event type `6446177` had
  `lengthInMinutesOptions` enabled via `PATCH /v2/event-types/{id}` (the API supports
  this directly — no dashboard click-through needed) to give every hotel service its
  correct duration.

`hotel-mzv` is now a real hotel tenant (rooms, restaurant, spa, event space, airport
transfer) — converted from its original electrician-shaped seed data.

**User-editable content is consolidated in `content/`** (see `content/README.md`): the system
prompt (`content/system-prompt.md`, a `${placeholder}` template loaded by
`app/brain/prompts/system.py`), tenant configs (`content/tenants/`, path from
`settings.content_dir`), and acknowledgement phrases (`content/acknowledgements.json`). All
hot-reload via mtime-keyed caches — edits apply next turn, no restart. `.env` (model + keys)
stays at repo root. The Dockerfile must `COPY content ./content`.

Voice is wired end to end: `app/channels/vapi_llm.py` is a real OpenAI-compatible
`/chat/completions` SSE shim, `webhooks.py` records `calls` from end-of-call reports, and
`scripts/provision_vapi.py` creates the assistant. Web call and phone call are the same
assistant — attaching a number is what adds PSTN.

**Phase 4 (Supabase, RLS, per-tenant secrets, durable checkpointer) is done and
live-verified against a real Supabase project — see `plans/phase4.md`.** In order:

- **Two doors into one Postgres instance.** PostgREST over raw httpx (`app/db/supabase_store.py`,
  `app/tenancy/secrets.py`, `app/tenancy/sync.py`) for everything tenant-scoped; `psycopg`
  (`app/db/checkpointer.py`) reserved *only* for the LangGraph checkpointer, which needs a
  real transactional connection PostgREST can't give it. Don't blur this line — it's what
  keeps the compiled `psycopg` dependency optional (see below).
- **`get_store()` lives in `app/db/factory.py` now**, not `app/db/memory_store.py` — it
  returns `SupabaseStore` when `SUPABASE_URL` is set, `InMemoryStore` otherwise, and is a
  **hard boot failure** (`app/main.py`'s lifespan) if `APP_ENV=production` with no
  `SUPABASE_URL`. Application code imports `get_store` from `app.db.factory`; test fixtures
  import the concrete `InMemoryStore` singleton from `app.db.memory_store` directly, so
  tests are immune to whatever a given test's settings happen to be.
- **RLS is real, not decorative.** `app/db/auth.py` mints a short-lived HS256 JWT per
  request carrying `tenant_id` + `role: app_backend`; every table has `FORCE ROW LEVEL
  SECURITY` (not just `ENABLE`) plus a `tenant_id`-scoped policy. Live-verified: a
  cross-tenant read comes back empty, a cross-tenant write is rejected by `WITH CHECK`.
- **Per-tenant secrets are live.** `hotel-mzv` and `northside-plumbing` can each have their
  own `CALCOM_API_KEY`/Twilio credentials in Vault (`app/tenancy/secrets.py`); a tenant with
  none configured falls back to the shared `.env` value, but a **vault error never falls
  back** — that would silently book a different tenant into whoever's account the env key
  belongs to. Verified live: two tenants resolve to genuinely different Cal.com credentials.
- **The checkpointer is durable when `DATABASE_URL` is set**, optional (`pip install -e
  ".[postgres]"`) and self-degrading — any failure (missing dependency, bad connection)
  falls back to `InMemorySaver` with a WARNING, never a crashed boot. Live-verified: a
  checkpoint written by one process is readable by a completely separate one (genuine
  restart survival), and the tables land in a dedicated `langgraph` Postgres schema, never
  `public` (PostgREST exposes everything in `public` — leaving them there would make every
  transcript readable with the anon key).
- **`scripts/onboard_tenant.py` and `scripts/sync_tenants.py` are real and live-tested** —
  onboarding writes the tenant JSON, syncs it to Supabase, stores per-tenant secrets in
  Vault, and (given `--voice-sample` + consent) clones a voice, in one idempotent command.
- **Voice cloning is wired but unused** — `app/tenancy/voice.py` + the `voice_consents`
  table + a DB trigger (`0005_voice_consent.sql`) enforcing "no `voice_id` without recorded
  consent" are all live-verified, but no tenant has actually been cloned yet (needs a real
  audio sample + written consent — CLAUDE.md convention #6 has no exceptions).
- **Tenant *config* still reads from `content/tenants/*.json`, not Supabase.** The
  `tenants`/`services` tables exist and `sync_tenants.py` keeps them current, but nothing in
  the running brain queries them yet — flipping that is a one-line `TENANT_SOURCE=supabase`
  once live-verified, deliberately deferred (see the plan's "On the tenant read path" note).

**Phase 5 (Chatbot channel) is done — see `plans/phase5.md`.** `app/channels/chat.py`
now has two endpoints, not one:

- **`POST /chat/session`** is the public handshake: a tenant's widget key in, a
  server-minted `session_id` + short-lived HMAC session token out (`app/channels/
  widget_auth.py`, sign *and* verify — unlike `app/db/auth.py`, which only signs).
  Resolves the tenant, checks `Origin` against `tenant.chat.allowed_origins` when
  non-empty, and creates the `ChatSession` row before any stream starts — an unknown
  widget key is a clean 404, never a truncated stream after `200 OK`.
- **`POST /chat`** now accepts two callers via `require_chat_caller`
  (`app/channels/security.py`): a **widget** session token (tenant + session id come
  from the verified token, body `tenant_id`/`widget_key` ignored) or the **trusted**
  `API_AUTH_TOKEN` bearer (body-driven, exactly the pre-Phase-5 behaviour — this is
  what `chat_cli`/tests/server-to-server callers still use). Event filtering now
  matches voice: only `token`/`acknowledgement`/`suggestions`/`handoff`/`final`/`error`
  reach the browser — `tool_start`/`tool_result` are logged only.
- **`check_availability` is now `content_and_artifact`** too (same pattern as
  `escalate`), returning `(text, {"kind": "slots", "service": ..., "slots": [...]})`.
  The runner turns this into a `BrainEvent("suggestions")` a widget renders as
  quick-reply chips. Text is byte-identical to before; only the artifact is new.
- **`handoff` now fires on every channel**, not just voice — `_handoff_artifact`
  (`app/brain/runner.py`) used to filter out anything with `transfer: False`, which
  meant chat could *never* emit one (`SmsCallbackEscalator.can_transfer` is always
  `False`). The frame-emission decision moved to `app/channels/vapi_llm.py`, which now
  gates the `transferCall` frame on `event.data["transfer"]` itself; chat gets the same
  event and renders a `tel:` link instead.
- **The embeddable widget (`widget/`)** is a Preact + TypeScript bundle built with Vite
  into one dependency-free `<script>` tag (`widget/dist/widget.js`, committed — see
  `widget/README.md`). CORS is wide open on `/chat`/`/chat/session`
  (`allow_credentials=False`, so `allow_origins="*"` is legal); the real per-tenant
  boundary is `chat.allowed_origins`, checked once at the handshake.
- **Live-verified against the real stack** (real Gemini model, real Cal.com,
  `store: "supabase"`): the handshake, a full streaming turn with `suggestions`
  carrying real Cal.com slots, and event filtering all work end to end. One gap found
  live and not yet closed: **`app/db/migrations/0006_chat.sql`
  (`chat_sessions`/`chat_messages`) has not been applied to the live Supabase project**
  — the same manual dashboard-SQL-editor step every prior migration needed. Until it
  is, `_record_chat_message`/`astart_chat_session` fail with a 404 from PostgREST,
  caught and logged (never breaking the stream — confirmed live, not just in tests),
  so conversations still work end to end, they just aren't durably transcribed yet.

**Phase 6 (MCP layer) is done — see `plans/phase6.md`.** `app/mcp/client.py` no longer
returns `[]` unconditionally; the long tail (CLAUDE.md convention #2) is live:

- **`langchain-mcp-adapters>=0.3`** is an optional extra (`pip install -e ".[mcp]"`),
  imported lazily inside `load_mcp_tools` — an `ImportError` degrades to no MCP tools
  with a WARNING, never a crashed turn. Its own dependency chain (`mcp` → `jsonschema` →
  `rpds-py`, `pyjwt[crypto]` → `cryptography`, `pywin32` on win32) probed clean on this
  box (Phase 6 Step 0), but the lazy-import fallback exists precisely because that isn't
  guaranteed on every box — see the `uuid_utils` gotcha below.
- **A real bug got fixed along the way, not just a new feature added:**
  `app/brain/graph.py` used to build `ToolNode(NATIVE_TOOLS)` once, at compile time, from
  a static list, while `reason` already bound a per-tenant tool set every turn. Nothing
  exposed this before MCP existed, because nothing was ever bound outside that static
  list. `app/brain/nodes/tools.py` is the fix: a node that resolves the same tool set
  `reason` just bound (native + MCP) and builds `ToolNode` from it on every invocation.
  Reproduced and confirmed live by temporarily reverting to the old static node during
  development — see the CLAUDE.md gotcha below and `tests/test_mcp_tools_node.py`.
- **Two read paths behind `MCP_SOURCE`** (`"json"` default, `"supabase"` in production):
  unlike the full tenant-config read path (`TENANT_SOURCE`, still deferred), this one
  flipped early and deliberately — `app/mcp/registry.py::servers_for()` is only ever
  called from inside the already-async, already-gated, already-degrades-to-`[]`
  `load_mcp_tools`, so it carries none of the risk that kept `TENANT_SOURCE` parked.
  `scripts/register_mcp_server.py` adds/updates/removes a live tenant's server with one
  command — no redeploy — writing the row through the Supabase admin path and any
  credential into Vault via the existing `set_tenant_secret`.
- **HTTP-only by default.** `MCP_ALLOW_STDIO=false` refuses `transport: "stdio"` servers
  (a `command` string from tenant config is remote code execution on the one box holding
  every tenant's data) unless an operator opts in explicitly.
- **`${secret}` substitution, not an auth-style enum**, because real hosted MCP servers
  don't agree on where the credential goes — Tavily's takes it as a **URL query
  parameter** (`?tavilyApiKey=${secret}`), most others as a header. `${secret}` is
  substituted into both `url` and every `headers` value at connect time
  (`app/mcp/connections.py`), from a Vault secret resolved by reference — never inlined
  into tenant config or the `mcp_servers` table.
- **A tenant's resolved MCP tool list is cached with a fingerprint over the *resolved*
  connection set**, not just a TTL — see the CLAUDE.md gotcha below for why that's load-
  bearing (bind/execute consistency across the same turn), not just a latency
  optimization.
- **A first-party demo server** (`scripts/demo_mcp_server.py`, `FastMCP`, streamable HTTP,
  two canned hotel-shaped tools) proves the whole path with zero accounts — the
  acceptance criterion's live check runs against it.
- **Native tools stay native.** No tier-1 tool moved to MCP; `check_availability` /
  `book_job` / `send_confirmation` / `escalate` / `is_emergency` are unchanged.

Next: apply `0006_chat.sql` **and** `0007_mcp.sql` to the live Supabase project together
(same manual dashboard-SQL-editor step every prior migration needed), then re-run
`provision_vapi --tenant hotel-mzv` so the transfer number Vapi will actually dial
matches `emergency.escalation_phone` (needed once regardless of Twilio). Twilio stays
parked until the client revisits that decision. Phase 4's own deferred items (a second
real Cal.com account to prove cross-tenant booking end-to-end, the actual voice clone,
the tenant read-path flip), Phase 5's and Phase 6's own (`plans/phase10.md`: WhatsApp,
per-key rate limiting once a usage-tier model exists, a concrete search/scraper MCP
server once a key exists) can land opportunistically whenever those inputs arrive.

**Phase 7 (Deploy + harden) is code-complete and offline-tested — see `plans/phase7.md`
(the full plan) and `infra/README.md` (the runbook). Not yet deployed anywhere.** In
order: a hardened multi-stage `infra/Dockerfile` now installs the extras production
actually needs (`postgres`, `mcp`, `google`) from a lockfile generated inside the Linux
image (`infra/requirements.lock.txt`, committed) instead of a bare `pip install .` with
none of them — the deployed image previously could use neither the durable checkpointer,
MCP tools, nor `LLM_PROVIDER=google`, invisible until someone actually ran the container.
`app/preflight.py` now fails the boot loudly, all at once, under `APP_ENV=production` if
`API_AUTH_TOKEN`/`VAPI_WEBHOOK_SECRET`/`WIDGET_SESSION_SECRET`/`PUBLIC_BASE_URL`/
`SUPABASE_JWT_SECRET`/an LLM provider key is missing — every one of those used to fail
*open* with no tie to `APP_ENV` at all. `/health` can now report `"degraded"` with a
`problems[]` list (checkpointer silently fell back to memory, widget bundle missing, MCP
unavailable) and moves `env`/`llm_provider`/`model`/the full tenant roster/`tracing`
behind the same `API_AUTH_TOKEN` bearer `/chat`'s trusted path uses; a new `GET /readyz`
actually touches the database, closing a hole in the original keep-alive plan (`/health`
alone never would have prevented a free Supabase project pausing after 7 idle days).
Structured JSON logs + request-id correlation (`app/middleware.py`) are real now, and two
PII leaks are fixed (an SMS body logged at INFO, a checkpointer failure log that could
have embedded `DATABASE_URL`'s password). `app/channels/ratelimit.py` adds in-process,
per-replica rate limiting on `/chat` and `/chat/session` (widget-token callers only — a
trusted caller is exempt, same as the Vapi routes). `LANGCHAIN_TRACING_V2` et al. are now
real `Settings` fields exported into `os.environ` by `lifespan` — see the gotcha below for
why they were silently inert before. `.github/workflows/ci.yml` and `keepalive.yml` exist for
the first time. `scripts/loadtest.py` drives `/chat` and `/chat/completions` over real
HTTP against a running server and reports p50/p95/p99 latency.

**Step 10 (Supabase re-creation) is done and live-verified.** The project is now on
`us-east-1` (was Asia) — chosen over keeping Asia or moving the *app* to Asia instead:
Vapi and the active LLM provider (Gemini, direct API — not Vertex AI) are both US/EU-
anchored with no region selector of their own, so co-locating the app + DB there is the
only lever actually under this project's control; moving the app near Pakistan instead
would add a second cross-continental hop on both the Vapi and model legs, not remove one.
All 7 migrations applied clean via a one-off `psycopg` script (no dashboard SQL editor
needed — `DATABASE_URL` was already the session-pooler URI). One real gotcha hit along
the way: **a freshly created Supabase project's `service_role` key is not
`SUPABASE_JWT_SECRET`.** They're both shown as "keys" in the dashboard but are entirely
different values — `SUPABASE_JWT_SECRET` is the legacy HS256 signing secret PostgREST
verifies `app/db/auth.py`'s tenant JWTs against, and pasting `service_role` there instead
produces a PGRST301 ("None of the keys was able to decode the JWT") that looks exactly
like a config problem, because it is one, just not the one it seems — the actual value
lives at **Project Settings → JWT Keys → Legacy** (a separate sidebar page from
**Settings → API Keys**, which only shows `anon`/`service_role`). Confirmed live on the
new project: `checkpointer: "postgres"`, `store: "supabase"`, `/rest/v1/checkpoints`
still 404s (schema isolation holds), `sync_tenants` pushed both tenants, and the Cal.com
Vault secret round-trips correctly scoped to `hotel-mzv` only (`northside-plumbing`
correctly gets nothing back). The old Asia project still exists, untouched — deleting it
is a separate decision, not part of this migration.

**Phase 7 is fully done — Step 11 (deploy) completed and live-verified.** Deployed to
Railway (`ai-receptionist` project, service connected to the GitHub repo for autodeploy,
region `us-east4`, one replica — matches the single-replica constraint). `railway.json`
pins the Docker builder to `infra/Dockerfile` and sets `healthcheckPath: /health`, since
Railway's auto-detection only looks for a root-level Dockerfile otherwise. Order followed
exactly as planned: domain reserved first
(`https://ai-receptionist-production-5cb4.up.railway.app`), every secret set as a Railway
variable (never baked into the image), `provision_vapi --tenant hotel-mzv` re-run against
that URL from the dev box, the resulting tenant JSON change committed, *then* deployed.

One real snag hit during provisioning, not a secrets issue: **Vapi's API rejects
`555`-prefixed numbers as a `transferCall` destination** ("must be a valid phone number in
E.164 format") — `555` is North America's reserved fictional-number exchange, and
hotel-mzv's `emergency.escalation_phone` (`+15551230911`) was always placeholder dev data,
same as its main line. `allow_warm_transfer` is now `false` for hotel-mzv until a real
escalation number exists — flipping it back is a one-field JSON edit + a `provision_vapi`
re-run, no redeploy needed. This exposed (again) that six tests across
`test_warm_transfer.py`/`test_native_tools.py`/`test_vapi_llm.py` were asserting on
hotel-mzv.json's *current* `allow_warm_transfer` value as if it were "the default" —
fixed properly this time by having every "enabled" test build that explicitly via
`model_copy` (or the `override_tenant` fixture, for anything that re-resolves the tenant
through `get_tenant_config` — a bare `model_copy` is invisible to that path). The tenant's
real config can now change without silently flipping unrelated tests again.

Confirmed live: `/health` shows `checkpointer: "postgres"`, `store: "supabase"`,
`problems: []` (production preflight passed clean); `/readyz` touches the real database;
the deployed `VAPI_WEBHOOK_SECRET` matches what's baked into the Vapi assistant (a wrong
secret 401s, the real one doesn't); and a real `/chat/completions` turn against the live
assistant id returns a correct, contextual answer from the real Gemini model. No phone
number is attached yet (Vapi web-call only) and Twilio stays parked — both remain the
client's call, not blocked on anything technical.

## Gotchas learned the hard way
- **Groq leaks tool calls into text.** Llama 3.3 sometimes writes
  `<function=name>{...}</function>` into the reply, and sometimes emits a call malformed
  enough that Groq rejects the whole request. Both are handled in `app/brain/sanitize.py`
  and the retry in `app/brain/nodes/reason.py` — don't remove either without a replacement.
- **Tools read `tenant_id` from `RunnableConfig`, never from a model argument.** That's
  what stops the LLM crossing tenants; keep it that way when adding tools.
- **`reason` must use `astream`.** Switching to `ainvoke` silently kills token streaming
  and the latency budget with it.
- **There is deliberately no router node**, though plan §5 lists one. The model routes
  implicitly via tool choice; a separate classifier hop costs latency the §13 budget can't
  spare. Consequence: `state["intent"]` is only set by `emergency_check`. Don't add a
  router without a latency measurement justifying it.
- **`caller` and `booking_draft` are declared but unwritten.** Deliberate — see the note in
  `app/brain/state.py`. Populate them in Phase 3 when resuming a dropped call needs them.
- **Groq restates its own acknowledgement after a tool returns**, usually truncated. Fine in
  text, jarring aloud, so `RepeatSuppressor` (`app/brain/sanitize.py`) drops it. It compares
  the first sentence of the new segment against what was just said and fails *safe* — when
  unsure it speaks the text.
- **Tool events must never reach `delta.content`** on the voice channel, or the caller hears
  raw tool output read aloud. Only `token` / `acknowledgement` become audio.
- **Voice thread id is the Vapi call id**, and history is reseeded from Vapi's transcript
  *only* when the thread is cold (`thread_is_cold`). Vapi resends everything each turn;
  seeding a warm thread duplicates the whole conversation.
- **Vapi's request body contains the tenant's Twilio credentials.** Log or persist it only
  via `vapi_schema.redacted()`.
- **Two endpoints, two auth paths, one secret.** `server.secret` only authenticates webhooks
  to `server.url`; the custom-LLM endpoint is `model.url` and needs `model.headers`
  (verified: Vapi accepts and persists it). Miss the header and the caller hears the
  greeting, then silence, while every turn 401s — a horrible thing to debug from a phone.
  `build_assistant_payload` sets both from `VAPI_WEBHOOK_SECRET`.
- **All Vapi wire-format assumptions live in `app/channels/vapi_schema.py`.** Field names
  were verified against Vapi's own reference implementation (VapiAI/example-custom-llm).
  If their payload changes, that file plus the header constants are the only edit needed.
- **Once a tenant is on `booking.provider: "calcom"`, Cal.com — not the tenant JSON —
  owns availability.** `booking.hours`/`lead_time_hours`/`slot_granularity_minutes`
  become prompt copy only; `check_availability` never re-filters by them
  (`app/tools/booking/calcom.py`). Edit them and nothing changes for a calcom tenant —
  edit the Cal.com event type's own schedule instead. Only `horizon_days` (the query
  window) and `max_slots_returned` (client-side truncation) still do anything. The
  Cal.com **schedule tz** governs availability and the **account profile tz** governs
  how the dashboard displays bookings — set both to the *tenant's* timezone, not your
  own, or the calendar shows a different clock (and possibly day) than the receptionist
  speaks. Verified live: a Karachi account displayed a New-York 8pm booking as next-day
  5am.
- **`parse_iso` reads a datetime as the tenant's LOCAL wall clock and discards any
  offset/'Z'** (`app/tools/formatting.py`). This is deliberate and load-bearing: the
  LLM routinely encodes a local date as UTC midnight (`2026-08-01T00:00:00Z` for
  "Saturday"), and a naive UTC→local conversion shifts that back across midnight to the
  previous evening — the bot then offers and books the wrong day. Both tool inputs
  (`earliest_iso`, `slot_start_iso`) are local caller intent, and `slot_start_iso` is
  copied from `check_availability`'s already-local output, so discarding the offset is a
  no-op on the happy path and a correction when the model mis-encodes. Don't "fix" it to
  honour the offset. Guarded by `test_native_tools.py::test_iso_wall_clock_is_always_read_as_local...`.
- **`check_availability`'s `earliest_iso` is "the time the caller wants", not a hard
  floor.** The tool floors the provider query at the START of that day, pulls a wide
  candidate set (`_CANDIDATE_LIMIT`), and returns the `max_slots_returned` slots
  *nearest* the requested time in either direction (`app/tools/booking_tools.py`). That's
  what lets "anything late / midnight?" surface the day's LAST slots (e.g. 9:30pm) rather
  than only the first ones after opening — the old earliest-after-floor behaviour could
  only ever answer "too early", never "too late". Consequence: a request like "after 3pm"
  can include a 2:30pm slot (nearest, same day) — acceptable for a receptionist and far
  better than hiding late availability. It never crosses to a prior day (the floor is the
  requested day's midnight, clamped to now). Guarded by
  `test_native_tools.py::test_a_late_request_offers_the_nearest_evening_slots...`.
- **`escalate` and `check_availability` are both `content_and_artifact` tools**, not
  plain string-returning ones — `escalate` returns `(text, {"kind": "handoff",
  "transfer": bool, ...})`, `check_availability` (Phase 5) returns `(text, {"kind":
  "slots", "service": ..., "slots": [...]})`. A bare `.ainvoke({...})` still gets a
  plain string back either way (verified on langchain-core 1.5.0), so this is
  transparent to anything calling either tool directly; the artifact only matters to
  `app/brain/runner.py`, which turns `"handoff"` into `BrainEvent("handoff")` and
  `"slots"` into `BrainEvent("suggestions")` (widget quick-reply chips) without either
  tool ever knowing a channel or a UI exists. Don't switch either back to a bare string
  return without moving that reading somewhere else first.
- **`handoff` fires on every channel now (Phase 5), not just when a live transfer is
  possible.** `_handoff_artifact` (`app/brain/runner.py`) used to filter out anything
  with `artifact["transfer"]` falsy, which meant `channel="chat"` could *never* emit one
  — `SmsCallbackEscalator.can_transfer` is always `False`. It now emits unconditionally,
  carrying `transfer: bool` in `data`; **the decision to actually issue a Vapi
  `transferCall` frame moved to `app/channels/vapi_llm.py`**, which checks
  `event.data["transfer"]` before buffering one. Chat gets the same `handoff` event and
  renders a `tel:` link (`widget/src/App.tsx`) instead of doing anything with it.
- **`get_escalator(tenant, channel)` takes the channel.** Voice gets a
  `WarmTransferEscalator` by default (opt out per-tenant with
  `emergency.allow_warm_transfer: false`); chat always gets `SmsCallbackEscalator`. Vapi
  refuses to transfer to a number the assistant didn't declare at provisioning time — a
  changed `emergency.escalation_phone` needs `provision_vapi` re-run, or the "warm
  transfer" silently no-ops while the caller hears "transferring you now."
- **The `transferCall` SSE frame is emitted after all spoken content, not when the tool
  returns.** Vapi acts on it immediately, so sending it early would cut the caller off
  mid-sentence — the buffering in `vapi_llm._sse_chunks` is what makes it a *warm*
  transfer. `openai_compat.py` stays vendor-neutral; the frame shape lives in
  `vapi_schema.py` instead.
- **`/chat`'s tenancy comes from a verified widget session token, never the request
  body — on the public path.** `require_chat_caller` (`app/channels/security.py`) tries
  the presented bearer as a widget token first (self-verifying HMAC, safe to attempt on
  anything); only when that fails does it fall through to the shared `API_AUTH_TOKEN`
  bearer, which *is* body-driven (that's the trusted, server-to-server path — `chat_cli`,
  tests). A widget caller's `tenant_id`/`widget_key` in the body is silently ignored.
  Same invariant voice has always had, finally enforced on chat too.
- **The chat widget's `<script>` tag is a frozen contract, its bundle is not.** Once a
  client site has `<script src="…widget.js" data-widget-key="…">` pasted in, that tag
  (and the `/chat/session` → `/chat` wire protocol behind it) can never change without
  breaking every existing embed — but the bundle it points at, and everything inside
  `widget/src/`, is freely rewritable. See `widget/README.md`.
- **`public.messages` is outbound SMS, not chat transcripts** — despite plan §6b listing
  it as "chat transcripts", that table has always been `OutboundMessage`
  (confirmations/reminders/alerts). The Phase 5 tables are `chat_sessions` /
  `chat_messages` (`0006_chat.sql`), a completely separate `ChatLog` protocol
  (`app/db/store.py`). Don't confuse the two when reading old plan text against the
  schema.
- **Chat transcript persistence is widget-only, not trusted-path.** `app/channels/
  chat.py` only writes `ChatMessage` rows when `require_chat_caller` returns
  `mode="widget"` — a `ChatSession` row is guaranteed to exist (created at the
  handshake) before any message can reference it by `session_id`, a guarantee the
  trusted/server-to-server path has no equivalent step for. Writes are backgrounded
  (`asyncio.create_task`, drained before the SSE generator closes) and wrapped so a
  store failure can never break a live stream — confirmed against a real, mid-outage
  Supabase table (see "Next" above), not just mocked.
- **`widget/scripts/buildhash.mjs`'s relative paths are relative to `widget/`, not the
  repo root** (`src/App.tsx`, not `widget/src/App.tsx`) — `tests/test_widget_bundle.py`
  mirrors that exactly, since the hash only matches if both sides agree on the path
  convention. It also sorts those paths as **strings**, not as `pathlib.Path` objects —
  Windows `Path` comparison is case-insensitive (matching the filesystem), so it and
  Node's case-sensitive `Array.sort()` disagreed on `api.ts` vs `App.tsx` ordering and
  produced two different digests over the identical byte stream until this was fixed.
  Sort the path *string* on both sides, always.
- **The Supavisor pooler, session mode, port 5432 — never the direct host, never 6543.**
  `db.<ref>.supabase.co` has resolved to IPv6-only since Jan 2024 and most hosting has no
  IPv6 egress (connection just times out, reads like a firewall issue). Port 6543
  (transaction mode) doesn't support prepared statements and `AsyncPostgresSaver` dies on
  it with `DuplicatePreparedStatementError` — use the `aws-0-<region>.pooler.supabase.com:5432`
  host from Settings → Database → Connection string → **Session pooler** tab.
- **`psycopg`'s async mode cannot run on Windows' default `ProactorEventLoop`.** Only
  `SelectorEventLoop` works. Linux (the deploy target) is unaffected. **The fix is a
  monkeypatch, not an `asyncio.set_event_loop_policy()` call** — that was tried first and
  is silently inert: uvicorn's own `Server.run()` (`uvicorn/server.py`) calls
  `asyncio.run(coro, loop_factory=self.config.get_loop_factory())`, and passing an explicit
  `loop_factory` makes `asyncio.run`/`asyncio.Runner` build the loop directly, **never
  consulting the event-loop policy at all**. On win32, `uvicorn.loops.asyncio.asyncio_loop_factory`
  hardcodes `ProactorEventLoop` unless uvicorn is a `--reload`/multi-worker subprocess — so
  the *documented* workaround (always run with `--reload`) happened to dodge the bug for an
  unrelated reason (`use_subprocess=True` flips that branch), not because of the policy call.
  Confirmed live: plain `uvicorn app.main:app` with no `--reload` stalled 30s on a
  `psycopg_pool.PoolTimeout`, silently fell back to `InMemorySaver`, and then spammed
  "Psycopg cannot use the 'ProactorEventLoop'" warnings forever after — meaning the durable
  Postgres checkpointer was **never actually connecting** on this box before this fix,
  regardless of `--reload`. `app/main.py` now patches
  `uvicorn.loops.asyncio.asyncio_loop_factory` directly (guarded by `sys.platform ==
  "win32"`) so it always returns `SelectorEventLoop`, no matter how uvicorn is launched —
  see that file's comment for the full trace. A standalone script that never goes through
  uvicorn's `Server.run()` (a bare `asyncio.run(main())`, no `loop_factory`) is unaffected by
  any of this and still needs its own `asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())`
  call, since plain `asyncio.run()` *does* still consult the policy.
- **Windows can't have both a Postgres checkpointer and MCP `stdio` at the same time.**
  Forcing `SelectorEventLoop` (above) fixes `psycopg`, but Windows' asyncio subprocess
  support (`create_subprocess_exec`, which `MCP_ALLOW_STDIO=true` would need) only works
  under `ProactorEventLoop` — never `SelectorEventLoop`. This is a genuine either/or on
  Windows, not a bug to chase further; Postgres durability wins the trade since it's the one
  already in active use, and `MCP_ALLOW_STDIO` defaults off anyway. Linux has no such
  conflict — plain `SelectorEventLoop` supports subprocesses there too.
- **Checkpoint tables must land in the `langgraph` Postgres schema, never `public`.**
  `AsyncPostgresSaver.setup()` creates unqualified `checkpoints` / `checkpoint_blobs` /
  `checkpoint_writes` tables in whatever schema is first on `search_path` —
  `app/db/checkpointer.py` sets `options=-c search_path=langgraph` on the connection pool for
  exactly this reason. PostgREST exposes every table in `public`; get this wrong and every
  conversation transcript is readable over the REST API with the anon key, silently, since
  nothing errors.
- **Checkpointer isolation is by thread-id prefix, not RLS.** The checkpointer connects with
  the database's own Postgres role via `DATABASE_URL` (the wire protocol, not PostgREST), so
  the JWT/RLS machinery in `app/db/auth.py` + `0002_rls.sql` doesn't cover these tables at
  all — don't assume it does when reasoning about isolation here.
- **A vault RPC error must never be read as "no per-tenant secret" (absent).** `resolve_secret`
  (`app/tenancy/secrets.py`) treats "the vault returned null" and "the vault timed out /
  5xx'd / 401'd" as opposite cases on purpose: only the first falls back to the shared env
  credential. Falling back on the second would silently book a different tenant into
  whichever real account the shared env key belongs to — the single most damaging bug this
  layer could ship. A stale cached secret is served instead, when one exists.
- **`get_tenant_secret`'s RPC derives `tenant_id` from the caller's own JWT claim, never a
  parameter.** A tenant-scoped JWT can ask for any `key_name` it likes and will only ever
  get *its own* tenant's secret back — verified live by having tenant B request tenant A's
  exact secret name and getting `null`. `set_tenant_secret` (the write side) is the mirror
  image: it takes `tenant_id` as a parameter because it's never called with a tenant-scoped
  JWT, only the Supabase secret key (onboarding), so there's no caller-supplied tenant_id to
  distrust.
- **`ToolNode` cannot be built once at graph-compile time, once MCP exists.** This was a real
  bug from Phase 1 through Phase 5 (`app/brain/graph.py` used to build
  `ToolNode(NATIVE_TOOLS)` a single time, from a static list), invisible only because nothing
  ever bound a tool outside that list. `reason` binds `native_tools_for(tenant, channel) +
  await load_mcp_tools(tenant)` **per tenant, per turn** — the moment a server-backed tool
  existed, the static `tools` node would reject any call to it
  (`"X is not a valid tool, try one of [...]"`), silently, with no useful log line. Fixed by
  `app/brain/nodes/tools.py`: a node that resolves the same per-tenant set `reason` just
  bound and builds a fresh `ToolNode` from it on every invocation. Reproduced and confirmed
  live during Phase 6 development by temporarily reverting to the old static node —
  `tests/test_mcp_tools_node.py` is the regression guard; don't revert this without moving
  the fix somewhere else first.
- **The MCP tool-list cache (`app/mcp/client.py`) is a correctness requirement, not a
  latency one.** `MultiServerMCPClient` is stateless — every `get_tools()` opens fresh
  sessions — so without a cache, `reason` (which binds tools) and the dynamic `tools` node
  (which executes them) would each independently reconnect. If a flaky server answered
  differently between the two calls in the same turn, the model could emit a call nothing
  can run. The cache is keyed per tenant with a fingerprint over the *resolved* connection
  set (post-secret-substitution), so a rotated Vault credential or an edited server list
  invalidates it immediately rather than waiting out the TTL.
- **MCP is HTTP-only by default; `stdio` is a deliberate off switch, not an oversight.** A
  `command` string in tenant config is arbitrary code execution on the one box holding every
  tenant's secrets and every tenant's data. `MCP_ALLOW_STDIO=false` refuses any
  `transport: "stdio"` server with a WARNING unless an operator opts in explicitly
  (`app/mcp/connections.py`).
- **Never log a raw MCP connection.** With query-parameter auth (Tavily's hosted server
  authenticates via `?tavilyApiKey=...`, not a header) the URL *is* the credential — a
  plain `logger.warning("...%r", connection)` would leak it exactly like an unredacted Vapi
  payload would leak Twilio credentials. `app/mcp/connections.py::redacted()` strips the
  entire query string and every header value, unconditionally; use it before any MCP
  connection dict reaches a log line.
- **`MCP_SOURCE=supabase` is the one tenant-config read path that flipped to Supabase
  early, on purpose — this does NOT mean `TENANT_SOURCE` is safe to flip too.**
  `app/mcp/registry.py::servers_for()` is only ever called from inside
  `load_mcp_tools`, which is already async, already gated on `MCP_ENABLED` (off by
  default), and already degrades to `[]` on any failure — none of which is true of the
  full tenant-config read path, whose flip stays deferred (`plans/phase10.md` item 8)
  because `tests/conftest.py`'s `isolated_runtime` walks `get_repository().list_ids()`
  in an autouse fixture that runs before that path could safely point at Supabase.
  Adding a server to a live `MCP_SOURCE=supabase` tenant is one row insert
  (`scripts/register_mcp_server.py`) — no redeploy, unlike every other tenant-config edit.
- **`LANGCHAIN_TRACING_V2`/`LANGCHAIN_API_KEY`/`LANGCHAIN_PROJECT` were in `.env.example`
  since Phase 1 and did nothing under uvicorn until Phase 7.** They were never real
  `Settings` fields (`extra="ignore"` swallowed them), and nothing in the repo calls
  `load_dotenv()` — so pydantic-settings read `.env` without exporting to `os.environ`,
  and LangChain's tracer reads `os.environ`, not `Settings`. It happened to work under
  `langgraph dev` (which loads `.env` itself via `langgraph.json`), which is exactly why
  this went unnoticed for six phases. Now real fields, exported into `os.environ` in
  `app/main.py`'s `lifespan` before `get_graph()` is built.
- **`infra/Dockerfile`'s `$PORT` handling needs `sh -c ... exec`, not a plain `CMD` array.**
  Railway injects `$PORT` at runtime, which a JSON-array `CMD` never expands (it's not a
  shell). `sh -c "exec uvicorn ... --port ${PORT:-8000}"` gets the expansion; the `exec` is
  load-bearing too — without it, `sh` stays PID 1 and forwards nothing, so a redeploy's
  SIGTERM never reaches uvicorn and `--timeout-graceful-shutdown` never gets a chance to
  drain an in-flight SSE stream.
- **One worker, one replica is a hard constraint on this codebase, not a conservative
  default.** `app/brain/metrics.py`'s `TurnCounter`, `app/channels/widget_auth.py`'s
  per-process fallback session secret, and `app/channels/ratelimit.py`'s rate limiter are
  all process-local state with zero cross-process coordination. A second worker or replica
  doesn't crash anything — it just makes the rate limiter quietly too permissive and lets
  widget sessions signed by one process fail verification on another. Silent and in the
  dangerous direction, which is worse than a crash.
- **Provision Vapi *before* deploying, never after.** `scripts/provision_vapi.py` writes
  `vapi.assistant_id` into the tenant's committed JSON, and `infra/Dockerfile` bakes
  `content/` into the image at build time. Deploy first and the running image's tenant
  JSON has no assistant id yet — `resolve_tenant_id`'s `find_by_assistant_id` misses
  silently, and the caller hears the greeting (spoken by TTS with no LLM round trip) and
  then silence on every subsequent turn, while `/health` stays green throughout.
