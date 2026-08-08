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
2. **Two tool tiers.** Critical path (`check_availability`, `book_job`, `send_confirmation`, `escalate`, `is_emergency`) = native, typed, validated tools. Long-tail integrations (Sheets, scrapers, CRM) = MCP. *Conditional* native tools (`search_knowledge`, `offer_actions`, `start_flow`, `offer_cards`) sit inside tier 1 but are bound only for tenants that configured them — see `native_tools_for`, and `ALL_NATIVE_TOOLS` for why the distinction is load-bearing.
3. **Tenant isolation.** Every table and every query carries `tenant_id`; enforce Supabase RLS as defense-in-depth. One tenant must never see another's data, MCP servers, or secrets.
4. **Provider-agnostic brain.** No vendor-specific logic in graph nodes. Swapping Vapi→Retell, Groq→OpenAI, or Google Calendar→Cal.com must not touch the graph.
5. **Secrets** live in env vars / Supabase Vault, never in code. Per-tenant credentials are encrypted.
6. **Voice cloning requires stored written consent.** No exceptions.

## Repo layout
See §18 of the plan: `app/brain` (graph, nodes, prompts), `app/channels` (vapi_llm, chat + widget_auth, webhooks), `app/tools` (native + `booking/` provider interface), `app/mcp`, `app/tenancy`, `app/db`, `app/main.py`, `widget/` (Preact/TS chat widget, bundled with Vite), `scripts/` (onboarding), `infra/`, `tests/` (incl. tenant-isolation + latency tests).

## Build phases
Follow §15 of the plan in order: 0 Prereqs → 1 Brain skeleton → 2 Vapi voice → 3 Real tools → 4 Multi-tenancy → 5 Chatbot → 6 MCP → 7 Deploy → 8 Admin/analytics. Each phase has an acceptance criterion — meet it before moving on. **Phase 8's scope changed from §15's original "avatar + analytics + admin"**: the video avatar moved to `plans/phase10.md` item 13 by client decision, and turned out to be well-timed — Vapi discontinued its Tavus integration (20 Jun 2025), so §12's premise for it ("Tavus, already integrated with Vapi") is stale regardless. Phase 8 shipped analytics + per-tenant admin only.

## Latency budget
Target 600–800ms end-of-speech → first audio (§13). Protect it with streaming + acknowledge-then-act, native tools on the critical path, and region co-location.

## Pending decisions (confirm with the user before they block you)
Per §16: whose voice to clone (+ consent). Booking provider is
decided — Cal.com (not the plan's original Google Calendar recommendation) — see
Current state. Chat is decided too: web widget only for now (Phase 5) — WhatsApp is
explicitly deferred, not undecided (`plans/phase10.md` item 4), pending a Twilio
WhatsApp sender and the same client go-ahead SMS itself is waiting on. Avatar is
decided too — wanted, but later (`plans/phase10.md` item 13), not Phase 8.

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
  carrying real Cal.com slots, and event filtering all work end to end.
  `app/db/migrations/0006_chat.sql` (`chat_sessions`/`chat_messages`) is **applied and
  carrying real rows** — transcripts are durable now. Worth keeping in mind anyway: while
  it was unapplied, `_record_chat_message`/`astart_chat_session` failed with a 404 from
  PostgREST that was caught and logged and **never broke the stream** (confirmed live).
  That degradation path is still the design — a store failure must never kill a live
  conversation — so a future table/permission problem will be silent in exactly the same
  way, visible only in the logs.

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

Next: **all nine migrations are applied** (Phase 7 Step 10 re-created the project with
`0001`→`0007`; `0008`/`0009` landed with Phase 8 — verified live, see that section). What
remains here is to re-run `provision_vapi --tenant hotel-mzv` so the transfer number Vapi
will actually dial matches `emergency.escalation_phone` (needed once regardless of
Twilio, and blocked on a real non-`555` number). Twilio stays
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

**Phase 8 (analytics dashboard + per-tenant admin) is done and live-verified —
see `plans/phase8.md`.** All nine migrations are applied to the live project and
Step 10's checklist passed against it: the five analytics views carry
`security_invoker=true` and return zero rows to the anon key while each tenant
JWT sees only its own row; **the acceptance criterion holds — a greeting edited
through `/admin` was served by the running app on the very next request, no
restart, no redeploy**; invalid edits 422 with per-field `loc` paths and a stale
`If-Match` 409s; the voice-consent gate 409s naming the exact `onboard_tenant`
command; **`0009`'s trigger fix is proven defused live** (an unrelated save on a
tenant that *has* a `voice_id` succeeds — see the gotcha below); removing a
service deletes the orphan row; `sync_tenants` refuses to stomp live config
without `--force` and `--export` round-trips semantically identical config; the
admin limiter 429s with `Retry-After`. The *deployment switch* — `ADMIN_ENABLED`
/ `ADMIN_AUTH_TOKEN` / `TENANT_SOURCE`, which must be set **together** or the
phantom-edit preflight crashes the boot in between — **is now set on Railway**
(done during Phase 9.1; confirmed 2026-08-08: production `/admin/api/session`
401s with `invalid or missing bearer token`, i.e. a real token is loaded, not
the unconfigured message, and production serves a tenant that exists only in
Supabase). Note the Railway `ADMIN_AUTH_TOKEN` and the local `.env` one are
different values; nothing requires them to match. Scope narrowed from plan §15's original
"avatar + analytics + admin"
by client decision — the video avatar moved to `plans/phase10.md` item 13, and a
Supabase-Auth per-tenant login mini-plan went in beside it as item 14.

- **The tenant read path can now genuinely be `TENANT_SOURCE=supabase`.**
  New `app/tenancy/supabase_repository.py::SupabaseTenantRepository` loads
  every tenant into an immutable snapshot **once at boot** (in `lifespan`,
  before `get_graph()`) plus a background refresh
  (`TENANT_SNAPSHOT_REFRESH_SECONDS`, default 300s) — never per-request I/O,
  since `TenantRepository.get()` is synchronous and every caller (graph
  nodes, every native tool, `resolve_tenant_id`) can't `await`. This is not
  new machinery so much as `JsonFileTenantRepository`'s exact caching
  pattern pointed at a different source. `content/tenants/*.json` is now
  **seed + degraded-mode fallback only** once this is on — never runtime
  truth in production. Still `"json"` by default (dev/test unaffected, zero
  test churn).
- **The reason this had to happen at all: the phantom edit.** An admin
  panel that edits config while `TENANT_SOURCE` stays `"json"` writes to
  Postgres via `sync_tenant()` and returns 200 — but the running app keeps
  reading the JSON files, so the receptionist never sees the change. Both
  halves work perfectly in isolation; nothing errors, nothing logs.
  `app/preflight.py` now refuses `ADMIN_ENABLED=true` with `TENANT_SOURCE=json`
  in production specifically to catch this.
- **The sync stomp — the mirror-image trap.** `scripts/sync_tenants.py`
  used to blindly upsert the whole JSON file over Supabase; once the admin
  panel can write there too, that silently reverts every panel edit. It now
  refuses to run against `TENANT_SOURCE=supabase` without `--force`, and
  gained `--export` to pull live config back down into the JSON files
  instead. `sync_tenant()` (`app/tenancy/sync.py`) also now **deletes**
  services/MCP servers the config no longer declares — it used to only ever
  upsert, so removing a service in the panel left an orphan row
  `check_availability` would keep offering forever.
- **Analytics reads never use the Supabase secret key.** `0008_analytics.sql`'s
  five views + one RPC (`tenant_metrics`) are all `security_invoker = true`
  and read through the *same* tenant-scoped JWT `SupabaseStore` already
  mints — deliberately, so a future logged-in tenant reading its own metrics
  runs the identical code path an operator's does. A `public` view without
  `security_invoker` runs with the *owner's* privileges and does **not**
  apply the underlying tables' RLS — `tests/test_migrations.py`'s lint is
  extended to fail any view missing it, since the original table-only lint
  wouldn't have caught this.
- **Admin auth fails *closed* — the one deliberate break with this file's
  own fail-open-when-unconfigured convention** (`app/channels/admin_auth.py`).
  Every other guard here (`require_chat_caller`, `require_vapi_secret`,
  `is_ops_caller`) treats an unset secret as "stay open, it's a dev box";
  `require_admin` 401s on every request when `ADMIN_AUTH_TOKEN` is unset,
  because the blast radius (every tenant's config, every transcript) is too
  large to default open. `ADMIN_ENABLED=false` (default) means the router
  isn't conditionally mounted at all — a router has no clean "un-include"
  against the one `app.main.app` object every test shares — instead
  `require_admin_enabled` 404s first, functionally identical to unmounted.
- **`AdminPrincipal` (`kind`, `tenant_ids`, `may_access`/`may_write`) is the
  whole "operator-only now, tenant login later" bet.** Every route depends
  on `require_admin`/`require_tenant_access`, never the raw token; the
  tenant id comes from the URL path, never the body; `_OPERATOR_ONLY_PATHS`
  (`app/tenancy/admin.py` — `tenant_id`, `status`, `phone_numbers`,
  `widget_keys`, `vapi`, `booking.event_type_id`, `voice.voice_id`,
  `mcp_servers`) ships now, inert, since every principal today is an
  operator. `plans/phase10.md` item 14 is the mini-plan for the real branch.
- **Whole-document `PUT`, shallow top-level merge — Pydantic is the entire
  validation layer.** `app/channels/admin.py::put_tenant` does `{**current,
  **payload}` at the top level only: submitting `{"greeting": "..."}`
  changes just that scalar, but submitting `{"voice": {"speed": 1.2}}`
  replaces the *whole* `voice` section, resetting any sibling field in it to
  `VoiceSettings`'s defaults. Deliberate, and matches how the admin UI
  actually submits sections (whole, never a sparse delta). No hand-written
  validation rule anywhere — `TenantConfig.model_validate`'s existing
  validators (`_calcom_tenants_declare_event_types`, `_unique_service_slugs`,
  `DayHours._close_after_open`, every `Field(gt=..., le=...)`, ...) are it;
  the route's only job is mapping `ValidationError.errors()` to a 422 whose
  `loc` tuples a UI attaches to inputs.
- **A real, pre-existing bug this phase's admin write path exposed:**
  `TenantConfig._real_timezone`'s validator called bare `ZoneInfo(value)`,
  and `zoneinfo.ZoneInfoNotFoundError` subclasses `KeyError`, which Pydantic
  does **not** catch and wrap into a `ValidationError` the way it does
  `ValueError`/`TypeError`/`AssertionError`. A bad timezone through any live
  API would have 500'd instead of 422ing — invisible until the admin write
  path became the first thing that actually exposed a bad-timezone input to
  a live caller. Fixed by catching and re-raising as `ValueError`.
- **The voice-consent trigger had an upsert blind spot — found before it
  ever shipped live, fixed in `0009_admin.sql`.** `0005_voice_consent.sql`'s
  trigger commented that it "only fires when voice_id is newly set or
  changed" — true for a plain `UPDATE`, false under
  `Prefer: resolution=merge-duplicates` (`INSERT ... ON CONFLICT DO UPDATE`),
  whose BEFORE INSERT pass sees `old = null` regardless of whether the
  voice_id actually changed. Any admin write to an unrelated field on a
  tenant with an existing cloned voice would have been rejected for "no
  voice_consents row" — invisible only because no tenant has ever been
  cloned. Fixed by comparing against a live self-select of the *currently
  stored* value instead of trusting `old`. `app/tenancy/admin.py::save_tenant`
  also pre-checks consent before writing (a clean 409 naming the exact
  `onboard_tenant` command) and maps a `P0001` trigger error the same way,
  covering the race between the two.
- **The credential-check-before-client-override bug, twice.**
  `SupabaseTenantRepository.refresh()` and `app/tenancy/admin.py::get_tenant_version`
  both originally checked `settings.supabase_url`/`supabase_secret_key`
  *before* checking whether a `client=` override was passed in — meaning an
  injected test client was unusable even though credentials were
  deliberately irrelevant to it. Both now check `owns_client and (not
  settings...)`, matching the pattern every other injectable provider in
  this codebase (`CalcomBookingProvider`, `TwilioNotifier`, `SupabaseStore`)
  already used.
- **A live-only bug offline tests structurally could not catch: two
  divergent `_admin_client()` builders.** `app/tenancy/admin.py` had its own
  copy of the Supabase client builder, missing the `Prefer:
  resolution=merge-duplicates,return=representation` header
  `app/tenancy/sync.py`'s version carries. `save_tenant()` passes its client
  into `sync_tenant(config, client=active)`, and `sync_tenant` only sets
  `Prefer` when building its *own* client — an injected one is used exactly
  as given (the same convention that lets tests inject bare headers). Net
  effect: every real admin-panel save silently downgraded from an upsert to
  a plain `INSERT`, producing a live `23505` duplicate-key 500 on the very
  first edit to an existing tenant. `mock_http`'s mock doesn't simulate
  PostgREST's `Prefer`-dependent upsert-vs-insert behaviour, so every
  offline test (both files inject their own client, bypassing either
  builder entirely) stayed green throughout — only a real request against a
  real database exposes this class of bug, which is the whole reason Step
  10 (live verification) exists rather than being skippable. Fixed by
  deleting the duplicate and importing `app/tenancy/sync.py`'s
  already-correct one; `tests/test_admin_write.py` now asserts on the real
  builder's headers directly (no mock in the loop) so this can't regress
  silently again.
- **`admin/` mirrors `widget/`'s conventions, but isn't an embed contract.**
  Preact + TypeScript, a committed `dist/` guarded by a `.buildhash`
  (`tests/test_admin_bundle.py` — a second artifact with the same "skip
  means not proof" caveat as the widget's own guard), no chart library, no
  routing library. Unlike `widget/`, it's a plain Vite SPA build (hashed
  asset filenames, not library mode) since nothing pastes a `<script>` tag
  pointing at it — served same-origin from `app/main.py`'s own
  `StaticFiles` mount, which must be the mount **and** the SPA catch-all
  route registered **after** `admin.router`, since `/admin/{path:path}`
  structurally matches `/admin/api/session` too (`{path:path}` captures
  slashes) and Starlette matches in registration order, not by specificity.
  `tests/test_api.py`'s route-ordering test is the actual proof, not an
  assumption. `StaticFiles(directory=...)` also raises **at mount time**
  (i.e. at boot) if the directory doesn't exist, unlike `/widget.js`'s
  deliberate 404-with-a-pointer — guarded with `if
  ADMIN_ASSETS_DIR.is_dir()`.
- **A genuine Windows-box-only build gotcha, same class as `uuid_utils`:**
  a fresh `npm install` in `admin/` resolves the newest `4.x` rollup, whose
  native `@rollup/rollup-win32-x64-msvc` binary is a file this Application
  Control policy hasn't trusted yet (`ERR_DLOPEN_FAILED` /
  *"An Application Control policy has blocked this file"*). `widget/`'s
  committed lockfile happens to pin the older, already-trusted
  `rollup@4.62.2` — `admin/package.json` pins `vite` to the same `5.4.21`
  and adds `"overrides": {"rollup": "4.62.2"}` for the same reason. See
  `admin/README.md`.

**Phase 9 Part A (Cal.com through MCP) is done and live-verified —
see `plans/phase9.md`.** `booking.provider: "mcp_calcom"` is a config flip
from `"calcom"` that reaches the same Cal.com account through its official
hosted MCP server (`https://mcp.cal.com`, OAuth 2.1) instead of the REST API,
with byte-identical conversational behaviour — the swap is at the provider
layer (`app/tools/booking/mcp_calcom.py::McpBookingProvider`), not the tool
tier, so `check_availability`/`book_job` stay the exact native tools they
always were; no tier-1 tool moved to MCP. `app/mcp/oauth.py` is the OAuth 2.1
client (PKCE + Dynamic Client Registration + headless refresh);
`scripts/authorize_calcom.py` is the one-time interactive grant per tenant.
The **Step A0 spike ran live against the real hosted server** (not simulated)
and both gates named in the plan are open: DCR is unauthenticated and
returns a public client with no secret, and `grant_types_supported` includes
`refresh_token` — see `app/mcp/oauth.py`'s module docstring for the full
recorded exchange.

**Live verification (2026-08-01) is done, against a real `hotel-mzv`-account
grant on a scratch tenant (`hotel-mzv-mcp-test`), not just offline tests:**
`authorize_calcom` completed a real interactive OAuth grant; a real
`check_availability` call through the widget path (`/chat/session` + `/chat`)
returned real Cal.com slots and rendered as a real `suggestions` event; a
real booking went through `create_booking` end to end (`book_job` →
`send_confirmation`, no `FALLBACK_LINE`); and that booking, pulled back from
Cal.com's own `/v2/bookings` API and compared directly against an equivalent
`"calcom"`-provider booking, matched on event type, duration, the
`caller-<digits>@example.com` placeholder-email pattern, and metadata shape
— **both previously-guessed tool schemas (`get_availability`,
`create_booking`) are now confirmed, not assumed.** `cancel_booking` /
`reschedule_booking` remain unverified (nothing calls them yet —
`plans/phase10.md` item 5). A warm-state `check_availability` p50 came back
effectively tied with the REST provider (~620ms both, comfortably inside the
§13 600–800ms budget) — the one-time first-call cost (session handshake +
OAuth token fetch, observed several seconds) is exactly what the per-tenant
session cache exists to amortize away. One real gap this session surfaced
and fixed: `app/main.py`'s `lifespan` closed pooled HTTP clients on shutdown
but had no equivalent for cached MCP sessions — `aclose_calcom_mcp_sessions()`
now runs alongside `close_shared_clients()`; without it, a process shutdown
left a live `streamablehttp_client`/`ClientSession` pair for the garbage
collector to tear down outside its owning task, observed live as
"attempted to exit cancel scope in a different task" / "generator is already
running" noise (harmless, but a real resource-cleanup gap, not cosmetic).

**Phase 9 Part B (bot lifecycle) is code-complete and offline-tested — see
`plans/phase9.md`.** An operator can now create, archive, restore and purge
bots entirely from `/admin` — `scripts/onboard_tenant.py` is no longer the
only creation path. `app/tenancy/models.py::TenantConfig.tenant_id` gained a
`^[a-z0-9][a-z0-9-]{1,47}$` validator (mirroring `McpServerConfig`'s tool-name
one) and `status` gained `"archived"`; `resolve_tenant_id`
(`app/tenancy/loader.py`) refuses an archived tenant on every channel via the
new `TenantArchivedError` (a `TenantNotFoundError` subclass, so every
existing `except TenantNotFoundError` handler already covers it with no code
change — `vapi_llm.py` needed one new `try/except` of its own since it never
had one at all before this). `app/tenancy/admin.py` gained `create_tenant`
(refuses a duplicate id, writes `"onboarding"` then the final status —
matching `onboard_tenant.py`'s ordering), `set_tenant_status` (archive/
restore, a pure status flip via `sync_tenant`, not `save_tenant` — no
version/consent checks apply), and `purge_tenant` (FK-ordered deletes,
refuses unless already archived, best-effort Vault/Vapi/JSON cleanup after).
`0010_lifecycle.sql` adds the status constraint and a `delete_tenant_secrets`
Vault RPC — deliberately **no** `grant delete ... to app_backend`; purge runs
on the Supabase secret key like every other admin write, and that role
already has `DELETE` via the project's own default-privilege grant (the
usual "trap" documented below, here working in purge's favor). Five new
bot templates live in `content/templates/*.json` (hotel, clinic, trades,
salon, restaurant). The admin UI (`admin/src/views/NewTenant.tsx`, plus a
Danger Zone at the bottom of `Config.tsx`) builds and serves correctly
(verified: TypeScript compiles clean, the bundle serves the new UI text over
HTTP) but has **not been visually clicked through in a real browser** — the
Chrome extension wasn't connected this session, so this is confirmed by
build + route tests (42 of them, `tests/test_admin_tenant_crud.py`), not by
eyes on the actual rendered page. Worth a real click-through before calling
Part B fully done. `_PURGE_TABLES` covers the current schema only
(`chat_messages` → `chat_sessions` → `escalations` → `messages` → `jobs` →
`calls` → `services` → `mcp_servers` → `voice_consents` → `tenants`) —
Part C's `knowledge_chunks`/`knowledge_documents` don't exist yet and will
need adding to the front of that list when they do — **done** as part of
Part C's own migration, see below.

**`0010_lifecycle.sql` is now live-verified (2026-08-02), not just
offline-tested.** It sat unapplied against the real Supabase project through
this whole write-up above — `tenants_status_check` still only allowed
`active`/`paused`/`onboarding`, so any real `set_tenant_status(...,
"archived")` call 400'd on the CHECK constraint, invisible until something
actually tried to archive a tenant against the live database. Caught doing
exactly that: an attempt to archive the `hotel-mzv-mcp-test` scratch tenant
(the Part A live-verification leftover, see `scripts/authorize_calcom.py`'s
section above) for cleanup. Applied via the same one-off `psycopg` /
`DATABASE_URL` approach Step 10 used for `0001`-`0009`; confirmed live
afterward (`tenants_status_check` now includes `'archived'`) — and then the
scratch tenant itself was actually archived and purged through the real
`set_tenant_status` → `purge_tenant` path, the first live proof either
function works end to end, not just against `mock_http`.

**Phase 9 Part C (per-bot RAG knowledge base) is code-complete and
offline-tested — see `plans/phase9.md`.** An operator can give any bot a
document corpus from `/admin`'s new Knowledge tab (paste text, upload a
file, or crawl a URL) that `search_knowledge` retrieves from mid-turn.

- **`search_knowledge` is the first *conditionally bound* native tool in
  this codebase.** Every other native tool (`check_availability`, `book_job`,
  `send_confirmation`, `escalate`, `is_emergency`) is bound to every tenant
  unconditionally; `native_tools_for` (`app/tools/registry.py`) — a function
  that has been a no-op pass-through since Phase 1 — now appends
  `search_knowledge` only when `tenant.knowledge.enabled` is true, gated a
  layer above by `settings.knowledge_enabled` (`KNOWLEDGE_ENABLED`, off by
  default). Correctness rests on the same guarantee MCP tools established in
  Phase 6: `reason` (binds) and the dynamic `tools` node (executes) both call
  this one function, so there is no second list to fall out of sync —
  `tests/test_knowledge_tool.py`'s real proof of this is a full graph turn
  that both binds and executes the tool successfully, not a static
  comparison of two lists (a static comparison can't actually distinguish
  "both sites agree" from "both sites happen to agree today").
- **Two independent switches, not one.** `KNOWLEDGE_ENABLED` gates the
  feature repo-wide (admin routes + whether the tool can ever be bound for
  anyone); a tenant's own `knowledge.enabled` (`KnowledgeSettings`,
  `app/tenancy/models.py`) turns it on for that tenant specifically. Both
  must be true.
- **Storage is Supabase-only, no dev-mode JSON equivalent.** Unlike every
  other piece of tenant content in this repo, knowledge documents/chunks
  have no `content/tenants/<id>.json` counterpart and no in-memory-only dev
  path beyond `InMemoryStore` (tests only) — `knowledge_source: "supabase"`
  is declared as a `Literal` with one value specifically so a future
  Qdrant/Pinecone swap is an obvious seam (`app/db/store.py::KnowledgeStore`)
  rather than a silent default no one notices.
- **`0011_knowledge.sql` is the first migration to grant `app_backend`
  direct CRUD on tenant-owned rows**, not just the RLS-scoped access every
  other table gets — documented at length in the migration itself as a
  deliberate, narrow exception: a tenant deleting its own uploaded document
  is ordinary tenant-scoped CRUD (still fully RLS-bounded), unlike
  `purge_tenant`'s cross-table admin action. `match_knowledge_chunks` is a
  `security invoker` SQL function (implicit — `language sql` functions are
  invoker by default, unlike the analytics views which needed it stated
  explicitly) taking no `tenant_id` parameter at all; isolation comes
  entirely from RLS on the two tables it joins, the same posture
  `get_tenant_secret`'s RPC established in Phase 4.
- **A real gap this step's own scope surfaced and fixed, not a hypothetical
  left for later:** `_PURGE_TABLES` (`app/tenancy/admin.py`) — Part B's own
  list, written before these two tables existed, with a comment
  anticipating exactly this — did not include `knowledge_chunks`/
  `knowledge_documents`. The FK actually does cascade from `tenants` (so a
  purge was never going to leave an orphaned row), but the per-table row
  counts `purge_tenant` returns and logs for audit purposes would have
  silently under-reported what a purge actually deleted. Fixed by adding
  both to the front of the tuple (children before parents, matching the
  existing convention); `tests/test_admin_tenant_crud.py`'s
  `test_deletes_in_fk_order` / `test_a_failed_table_delete_raises_and_stops`
  now assert the updated order.
- **Chunking is pure Python, no tokenizer dependency** — `app/rag/chunking.py`
  approximates token count as `len(text)//4`, prefers paragraph then sentence
  then word boundaries, and keeps ~15% overlap between consecutive chunks
  trimmed back to a word boundary. **Cosine similarity is pure Python too**
  (`app/db/memory_store.py::_cosine_similarity`) — no numpy in the dependency
  tree, so `InMemoryStore`'s search is a linear scan + sort, fine at test/dev
  scale, replaced by pgvector's HNSW index (`0011_knowledge.sql`) in
  production.
- **Ingestion runs off the request path.** `app/rag/ingest.py` schedules
  extraction → chunking → embedding → storage via `asyncio.create_task`
  (matching the chat-transcript-write pattern from Phase 5) for text/URL
  ingestion, and `asyncio.to_thread` specifically around the CPU-bound
  PDF/DOCX extraction step — this app is single-worker/single-replica
  (documented below), so a synchronous multi-second extraction on the event
  loop would stall every other tenant's live conversation, not just the one
  uploading.
- **`app/rag/crawl.py` resolves and re-validates the target IP, not just the
  hostname**, rejecting private/loopback/link-local/reserved/multicast
  addresses — re-checked after every redirect, since a same-origin-looking
  URL can 302 into `169.254.169.254` or a Docker-internal address. This is
  the same SSRF concern `plans/phase10.md` item 12 already named for
  tenant-submitted MCP URLs, addressed here for tenant-submitted crawl URLs
  first because Part C shipped first.
- **Re-indexing is scoped to URL-sourced documents only, on purpose.**
  Pasted text and uploaded files are never persisted past their chunks being
  embedded — there's no raw source to re-chunk from — so the admin route
  409s for text/file documents naming the limitation, rather than pretending
  to support something it can't actually do. Only a crawled URL can be
  freshly re-fetched from `source_ref`.
- **Embeddings are unverified against a live account** — same honesty
  caveat class as Part A's initially-guessed Cal.com MCP tool schemas
  (which later turned out correct once live-verified). `app/rag/embeddings.py`
  implements Gemini's `batchEmbedContents` request/response shape and
  per-request `outputDimensionality` truncation per its documented contract,
  reusing the same real `GOOGLE_API_BASE`
  (`https://generativelanguage.googleapis.com/v1beta`) `scripts/check_model.py`
  already hardcodes — but this repo has not yet made a real embedding call.
  A shape mismatch would surface as a loud `EmbeddingError` on every
  ingestion (mapped to a document's `status="failed"`), never a silent wrong
  answer — but "loud failure" is not the same as "verified correct," and a
  live pass (a real upload through `/admin`, confirming `status: "ready"`
  and a real `search_knowledge` retrieval end to end) is still owed before
  trusting this with a real client's documents.
- **58 tests offline** (19 in `test_knowledge_store.py`, 11 in
  `test_rag_chunking.py`, 14 in `test_rag_extract.py`, 11 in
  `test_knowledge_tool.py`, 3 more in `test_api.py`'s `/health` coverage)
  covering both `KnowledgeStore` implementations, chunking edge cases
  (unicode, oversized paragraphs,
  zero-overlap, an unsplittable oversized token), every `extract_text`
  branch including malformed-file error paths, conditional binding on both
  channels, and — the one the plan calls out by name — that bot A can never
  retrieve bot B's chunks, proven at both the store layer and through the
  tool directly. **Not yet done, and honestly still owed**: a real
  `/admin` click-through in a browser (same Chrome-extension gap Part B
  flagged), a live embedding call, and a real crawl against a live URL.

**`0011_knowledge.sql` is now live-verified (2026-08-02)** — applied in the
same pass as `0010_lifecycle.sql` above, against the real Supabase project.
Confirmed directly against the live database, not just "the migration ran
without erroring": `knowledge_documents`/`knowledge_chunks` exist with RLS
both enabled and forced; `app_backend` holds exactly `select`/`insert`/
`update`/`delete` on both (and, checked specifically, **not** `anon`/
`authenticated` — the explicit revoke this migration's own header calls out
as non-boilerplate actually took); the HNSW index on `knowledge_chunks`
exists; `match_knowledge_chunks` and `delete_tenant_secrets` both exist as
functions; both `pg_cron` sweep jobs are scheduled. What's still **not**
live-verified: a real ingestion (paste/upload/crawl → embed → store) and a
real `search_knowledge` retrieval through the running app — the schema is
now real, the pipeline against it still isn't proven end to end.

**Phase 9.2 (deterministic flows, rich buttons, generic-template cards) is
code-complete and offline-tested — see `plans/phase9.2.md`. Not yet
live-verified.** The 9.2 slot was reassigned by client decision: the *voice
tester* 9.1 earmarked for it moves to **Phase 9.3**, unchanged in design
(`plans/phase9.1.md`'s preview section carries a renumbering note, and
`app/main.py::_resolve_test_mode` still mints-and-rejects `mode="voice"`,
now naming 9.3).

- **A real flow engine, not LLM re-entry.** `app/flows/` (resolver, render,
  cards) turns a `TenantConfig.flows` node — fixed `say` text plus button
  slugs — into `BrainEvent`s with **no model request at all**.
  `stream_turn` gained a `postback` parameter (`flow:<id>`, from
  `ChatRequest.postback`) and short-circuits into it *after* the
  draft-preview override resolves (so a "Preview draft" session navigates
  the draft's flows) and *before* the graph. This is the feature most
  tempted to live in a channel adapter; it deliberately doesn't
  (CLAUDE.md's own convention), which is what lets a flow turn inherit rate
  limiting, transcript persistence, the channel-enabled check and the draft
  override without reimplementing any of them.
- **Termination is a graph edge, not a prompt instruction.** `start_flow`
  (the model's way *into* a flow when someone types free text instead of
  clicking) returns a `kind: "flow"` artifact, and `app/brain/graph.py`'s
  new `_after_tools` conditional edge routes that to `END` instead of back
  to `reason`. The `tools → reason` edge had been unconditional since Phase
  1. This exists because "show exactly this text and these buttons, then
  STOP" is a request a model honours *most* of the time — the reference
  prompt that prompted this phase attempts it four separate times in
  capitals ("END OF TURN RULE", "STRICT TERMINATION RULE"). A scripted node
  that sometimes gets a chatty sentence bolted on is not deterministic in
  any useful sense.
- **Buttons and cards are model-authored and need NO configuration — this
  is the headline, and it reversed a Phase 9.1 invariant on purpose.**
  `offer_actions` originally took catalog `slugs`, which meant a bot could
  only ever offer buttons an operator had already typed into config; no
  prompt wording could produce one. The project's actual premise is the
  opposite ("the only input from my side should be an AI prompt"), so the
  tool now takes `{label, url}` / `{label, reply}` specs as well as
  `{slug}`, and `native_tools_for` binds it **and** `offer_cards`
  unconditionally on chat. `ui.buttons`/`ui.cards` are kill switches read
  *inside* the tools, not binding gates, so a tool schema can never vanish
  mid-conversation.
- **Losing the slug indirection is a real security trade-off, taken
  knowingly.** It was what guaranteed a URL from a poisoned knowledge chunk
  or a hostile tool result could never become a clickable `<a href>` on a
  *client's own* website. What replaces it is `app/flows/urls.py` — one
  shared validator, `http(s)` schemes only, plus an optional per-tenant
  `ui.allowed_hosts` (empty by default, since a bot that can only link
  where an operator pre-approved is also a bot that can't link to something
  useful it found). A button naming a catalog slug skips the allowlist:
  it constrains the model, never the operator. The widget validates nothing
  and assumes nothing — one validator beats two that can disagree.
- **The shared `${ui_rule}` prompt section is the other half.** Binding the
  tools is useless if the model isn't told they exist, and a prompt pasted
  in from another platform never mentions them. `_UI_RULE`
  (`app/brain/prompts/system.py`) is a fixed "How this chat looks" briefing
  — offer buttons instead of asking open questions, never paste a URL, use
  `reply` buttons for menus, use cards for lists with pictures — rendered
  into `content/system-prompt.md` via `${ui_rule}` *and* auto-appended to
  every override by `_augment`. Chat only: neither tool is bound on voice,
  so describing them there would invite a call to something that doesn't
  exist.
- **`ui.opening_turn` (on by default) exists because a model can't produce
  buttons without a turn to produce them in.** The greeting is static config
  text with no model involved, so a prompt-authored opening menu was
  impossible. When on, the widget runs one real turn as the panel opens and
  suppresses the static greeting bubble — matching how Botsify's first
  message actually works. Costs one LLM request per visitor who opens the
  widget, including those who never type, so `start_session` suppresses it
  whenever a configured `chat.menu_flow` exists (that renders instantly and
  free — there'd be nothing to buy).
- **One catalog, four render targets — still true, now as the *precision*
  path rather than the only one.** `TenantLink` gained `reply` and `flow`
  types (plus `value`/`flow` fields) and remains the single source of truth
  for anything an operator wants pinned exactly, resolved in one place
  (`app/flows/resolver.py::resolve_buttons`). `flow` buttons in particular
  cannot be model-authored by definition — a scripted node is something an
  operator declared.
- **`prompt_augmentation` exists because a pasted prompt has no
  `${links}`.** An operator pasting a script written for another platform
  gets a complete, well-written prompt containing none of the new
  placeholders — so the model would never learn its buttons exist, for
  exactly the bots most likely to have some. `auto_append` (the default)
  appends any section the *rendered* prompt is missing; `placeholder_only`
  never touches the text. Both ship, switchable per tenant, pending a
  client decision on which to keep — the admin AI Prompt tab shows a
  warning banner either way.
- **No migration.** `links`, `flows`, `cards` and `prompt_augmentation` all
  ride inside the `config` JSONB (outside `_TENANT_COLUMNS`,
  `app/tenancy/sync.py`), exactly as Phase 9.1's `links` note predicted —
  so `sync.py`, `supabase_repository.py` and the whole draft/deploy/version
  path needed zero changes.
- **A real pre-existing bug this phase surfaced and fixed:** `is_slow_tool`
  inverted its rule as "anything not in the fixed five `NATIVE_TOOLS` is
  slow" — correct for MCP tools (the case it was written for), wrong for
  every *conditional native* one. `offer_actions` had therefore been
  triggering a spoken "bear with me a second…" before an instant in-memory
  dict lookup since Phase 9.1, with its own docstring in `registry.py`
  asserting the opposite. Merely cosmetic in chat; on a flow node it would
  have prefixed a deterministic node's configured wording with a model-ish
  filler phrase — the exact thing the feature promises can't happen, which
  is how the test caught it. Fixed with a new `ALL_NATIVE_TOOLS` constant;
  `NATIVE_TOOLS` stays frozen at five so
  `test_critical_path_tools_are_all_native` keeps its meaning.
- **963 tests offline** (23 in `test_flows.py`, 11 in `test_flow_tools.py`,
  28 in `test_card_tools.py`, 34 in `test_action_tools.py`, plus
  `test_system_prompt.py`). The load-bearing ones: a flow turn makes
  **zero** LLM requests; a flow turn is visible to the *next* free-text turn
  (proven by disabling `_remember` and watching it fail, not by assuming);
  `start_flow` genuinely ends the graph; a bot with **no configuration at
  all** binds `offer_actions`/`offer_cards`, gets `${ui_rule}` in its
  prompt, and renders a model-composed button end to end through `/chat`;
  a `javascript:` URL and an off-allowlist host are both dropped while a
  catalog button survives; a voice-channel postback is ignored; a stale
  postback falls through to the model; and voice binds exactly the fixed
  five and nothing else.

**Deployed to Railway and live-verified in production (2026-08-07).** Pushed
to GitHub (`m-zainvazir/hotel-mzv`, **public** — see the botsify/ note in
.gitignore) which autodeploys. Confirmed against the production URL:
`/health` clean with `store: supabase` + `checkpointer: postgres`, `/readyz`
touches the real database, the new `/bot/{widget_key}` share page serves,
and a real chat turn emitted **`cards` + `actions`** — so model-authored
buttons and the card carousel work in production, not just locally.
`TENANT_SOURCE=supabase` is confirmed *empirically*: production serves
`playmouth2`, a tenant that exists only in Supabase and has no
`content/tenants/*.json` file at all. That closes Phase 9.1's last
deployment item.

**What is live-verified, and what still isn't:**

- ✅ **9.1**: migration 0012; draft → deploy → live on the next turn; the
  Versions tab end to end (revert changed the *running* bot's greeting on
  the next request, the Deployed badge moved, deleting a non-live version
  worked and deleting the deployed one 409'd — proven on a throwaway tenant
  that was then archived and purged); Test Agent + Preview draft links;
  `offer_actions` buttons; the Railway switch.
- ✅ **9.2**: card carousel, model-authored buttons/quick replies, the
  opening turn, the public share link.
- ✅ **The deterministic flow engine, live-verified 2026-08-07** against the
  real Supabase project with `checkpointer: "postgres"`, on a scratch tenant
  (`zz-flow-check`) since archived and purged. Each claim proven
  individually: `chat.menu_flow`'s buttons come back from the handshake (and
  correctly suppress `opening_turn`); a `flow:` postback renders the node's
  configured wording with its buttons in configured order at
  **`llm_requests: 0`**; the `aupdate_state` write-back survives real
  Postgres — a free-text turn in the same session repeated the flow's text
  **verbatim**, which is exactly what an in-memory-checkpointer test cannot
  demonstrate; `start_flow` routes free text into a flow at
  `llm_requests: 1` with nothing appended (a loop back to `reason` would
  have made it 2 plus a trailing sentence, so this is the graph edge
  working); a stale postback falls through to an ordinary model turn rather
  than dead-ending; and a postback naming another tenant's flow id does not
  cross over (playmouth2 answered from its own config).
- ✅ **`ui.allowed_hosts` live-verified** the same way: with
  `allowed_hosts: ["example.com"]` and a prompt instructing the model to
  offer both an on-list and an off-list URL, only the on-list button
  rendered, with a matching `rejected button url` WARNING from
  `app/flows/urls.py`.
- ✅ **Channel flags live-verified** both directions: `chat.enabled=false` →
  `/chat/session` AND `/bot/{key}` both 404; re-enabling restores 200 on the
  next deploy. (A first attempt appeared to show that re-enabling was
  impossible — that was the *test harness* mangling the default holding
  message's em-dash into a UTF-16 surrogate under Windows cp1252, which
  httpx then refused to encode. When driving this admin API from a shell on
  Windows, force `PYTHONIOENCODING=utf-8` and pass `encoding=` to every
  open() — otherwise a round-trip of any tenant config corrupts it.)
- ✅ **`escalate` live-verified on chat**: a "get me a human" turn emitted a
  `handoff` event carrying a real `escalation_id` + destination, and the
  `escalations` table recorded the row. The SMS half cannot fire — Twilio is
  deliberately off (`notifications.provider: "stub"`), a client decision,
  not a gap.

**Phase 9.1 and 9.2 are both complete and live-verified.** Open items are
quality/decision, not missing function:
- ✅ **The cross-tool-hop restatement is fixed** (Phase 9.3 Step 0 item 1) —
  `RepeatSuppressor` was rewritten to compare **per sentence** against every
  sentence spoken this turn. See the gotcha below for the two structural
  bugs and the three guards that let the threshold drop to 0.7 safely.
  Offline-proven (21 tests, five of which were verified to fail against the
  old implementation); **not yet confirmed on a live turn** — the remaining
  reworded-mid-stream case is documented, not solved.
- ⏸️ `prompt_augmentation` still ships both behaviours pending a decision —
  say "lock prompt augmentation to auto_append/placeholder_only".
- A `ListField` fix (every comma-separated field in the panel could only
  ever hold one item — the input re-derived its text from the parsed array
  each keystroke and ate the comma) is shipped but not yet clicked in a
  browser.
- Scratch tenants from 9.1/9.2 testing are still in the tenant list
  (`new-cringe-1`, `playmouth1`, `test-clinic`, `flow-test`) — archive/purge
  through the Part B lifecycle path when done with them.

## Gotchas learned the hard way
- **`/widget.js` must send `Cache-Control: no-cache`, never `immutable`.**
  It shipped as `public, max-age=31536000, immutable` from Phase 5 — the
  correct header for a content-hashed filename, and exactly wrong for this
  one. `/widget.js` *is* the frozen embed contract (widget/README.md), so
  the URL can never gain a hash: the path is fixed forever while the bytes
  change on every build. `immutable` tells a browser not to revalidate even
  on a normal reload, so a client site that loaded the widget once would
  serve that build for up to a year and any widget fix would reach nobody.
  The ETag (the build hash) was already correct and simply never used,
  because `immutable` means the conditional request is never sent. Found
  live: the Phase 9.2 card carousel appeared to "not render", with the
  server demonstrably emitting valid `cards` events and the built bundle
  demonstrably containing the component — the browser was running a
  months-old bundle. `no-cache` means "cache, but revalidate every time",
  so an unchanged bundle is a bodyless 304. Guarded by
  `test_api.py::test_the_widget_bundle_is_revalidated_never_cached_immutably`.
  Fixing the header is necessary but **not sufficient**: it does nothing for
  a copy already cached as `immutable`, which a browser will keep serving
  without asking. The Test Agent page therefore also renders
  `<script src="/widget.js?v={buildhash}">` (`app/main.py::_widget_build_id`)
  — a real client embed can't carry a query (the bare tag is the frozen
  contract) but that page is server-rendered on every load, so a changed
  bundle is a changed URL and an already-poisoned cache entry is bypassed.
- **Two debugging lessons from the above, both of which cost real time:**
  1. **"The UI doesn't show X" is not evidence the UI is wrong.** Server,
     SSE frame, stream parser, compiled event handler, render condition,
     component output and injected CSS were each verified correct in
     isolation before the actual cause was found. When every layer checks
     out, suspect what's *running* rather than what's *written*.
  2. **A request in the access log is not necessarily the user's.** A
     `GET /widget.js 200` was read as the browser fetching a fresh bundle
     when it was this session's own `curl`. The real signal was the
     *absence* of a request beside the page load. When reasoning from logs
     during a live debugging session, account for your own traffic first.
  A jsdom probe (mount the real `App`, mock `fetch` with a captured SSE
  body, drive the form) settles "is the widget code correct?" in one run
  without needing a browser at all — worth reaching for early next time.
- **A wrong return type in `admin/src/api.ts` silently disables the only
  guard that exists for the admin panel's wire contract.** Found live on the
  first real click-through of "New bot" (the one CLAUDE.md had flagged as
  owed since Phase 9 Part B): the bot was created correctly, then the panel
  navigated to `/tenants/undefined` and showed "no tenant config for
  'undefined'". `createTenant` was typed `Promise<TenantConfig>` — true when
  Part B wrote it, false once Phase 9.1 wrapped every lifecycle route in
  `_tenant_detail(...)` — so `created.tenant_id` compiled fine and was
  `undefined` at runtime. No Python test can see across the wire into
  TypeScript, and `tests/test_admin_tenant_crud.py` was *already* asserting
  the correct `response.json()["config"]` shape, so the server half was
  never wrong. **`tsc --noEmit` in `npm run build` is the regression guard
  for this whole class of bug, and an inaccurate annotation is what turns it
  off.** Verified by reintroducing the bug and watching the build fail with
  `Property 'tenant_id' does not exist on type 'TenantDetail'`. When a route's
  response shape changes, grep `admin/src/api.ts` for every function that
  returns it.
- **A flow turn bypasses LangGraph, so it must write itself back into the
  checkpointer.** `app/flows/render.py::_remember` calls `aupdate_state`
  with a `HumanMessage`/`AIMessage` pair after every scripted node. Omit it
  and *nothing fails* — no error, no log line; the model simply has amnesia
  about whatever the visitor clicked through, so "what were those options
  again?" gets answered from an empty transcript. It's the easiest line in
  the package to drop and the hardest consequence to notice, which is why
  `test_flows.py::test_the_flow_turn_is_visible_to_the_next_free_text_turn`
  exists and was verified to actually fail without it.
- **The `scripted` fixture calls `reset_graph()`, which throws away the
  in-memory checkpointer.** Any test spanning two turns that depends on
  conversation state must script *both* turns in one `scripted(...)` call —
  a second call between them silently wipes exactly the state under test
  and the failure looks like a product bug rather than a fixture artifact.
- **The boot-time tenant snapshot needs its own timeout, not `supabase_timeout_seconds`.**
  Found live during Phase 8 verification: `SupabaseTenantRepository.refresh()` shared the
  8s request-shaped budget, and a cold process — whose first HTTPS call pays DNS + TLS to
  `us-east-1` on top of the query — intermittently lost that race. The consequence is
  silent and in the worst direction: the app boots **fine**, serves every tenant from the
  baked-in `content/tenants/*.json` instead of Postgres, and keeps doing so until the next
  background refresh (`TENANT_SNAPSHOT_REFRESH_SECONDS`, 300s). On Railway that is every
  deploy, so a config edit made in `/admin` can look reverted for five minutes. Fixed with
  a dedicated `TENANT_SNAPSHOT_TIMEOUT_SECONDS` (20s) **plus one retry on a transport error
  only** — a 4xx/5xx is never retried, since a bad key or missing table can't be fixed by
  trying again and would just double the boot delay. The query itself takes ~0.4s warm;
  this is entirely about the cold first call. `/health`'s `problems[]` is the only signal
  when it does happen — the fallback is deliberately silent to the caller.
- **`str()` of an httpx timeout is the empty string.** The original snapshot failure logged
  `"failed wholesale: "` and named nothing, which is why the above took a live repro to
  diagnose at all. Log `type(exc).__name__` alongside `exc` anywhere an httpx error is
  caught and reported, not just `%s`.
- **Supabase's schema default privileges quietly re-grant everything in `public`.**
  `0008_analytics.sql`'s header says its views are granted "to app_backend ONLY — never to
  `authenticated`", and that is **not** what is live: `anon` and `authenticated` hold all
  privileges on all five analytics views, because the project has
  `ALTER DEFAULT PRIVILEGES ... GRANT ALL ON TABLES TO anon, authenticated, service_role`
  and every newly created view inherits it. `revoke ... from public` (which the migration
  does for the RPC) does **not** touch grants held by *named* roles. Isolation still holds
  — verified live, the anon key reads zero rows — but only because `security_invoker=true`
  pushes the base tables' RLS onto the caller. That means `security_invoker` is doing the
  work **alone**, not as one layer of two; drop it from a view and there is nothing behind
  it. Don't read the migration header as a description of the live grant state.
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
- **Models restate their own acknowledgement after a tool returns**, usually truncated.
  Fine in text, jarring aloud, so `RepeatSuppressor` (`app/brain/sanitize.py`) drops it.
  Its first version compared only **the first sentence of the new segment against the
  whole previous segment concatenated**, and both halves of that were wrong — this is
  what kept the defect alive at ~1/3 of turns after the prompt fix took it down from
  ~4/5:
  - *Targets must be individual sentences.* A restatement of one sentence scored poorly
    against the concatenation of all of them, so the similarity test never fired. This
    was the dominant live failure, and it got worse the more the model said before
    calling a tool.
  - *Every sentence of the new segment must be checked.* The echo routinely lands behind
    a short opener ("Sure. I can check with a bookseller…"), which a first-sentence-only
    check structurally cannot see no matter how good the comparison is.

  Lowering the threshold to 0.7 to catch reworded restatements needs three guards, or it
  eats real content — **"I've booked that for you" scores 0.85 against "I can book that
  for you"**, and that is the one sentence a caller must not miss:
  1. a truncation of something already said is dropped unconditionally (it contains
     nothing new by definition);
  2. anything introducing a **novel content word** is kept — "…for **Wednesday** too" is
     a new request, not an echo. This also protects most tense changes for free
     ("sent" ∉ "send");
  3. anything **reporting completion** (`i've|we've <verb>`, "all set", "is confirmed")
     is never dropped on similarity alone. `we've got` is excluded — inventory-speak,
     not completion.

  Still fails *safe*: when unsure it speaks. **Known limitation, deliberate:** a reworded
  restatement is only caught when enough of the sentence arrives to compare, because the
  fast path releases text the moment it can't be a prefix of anything — holding every
  sentence to its boundary would spend the §13 latency budget on every turn to fix a
  fraction of one.
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
- **The Cal.com MCP OAuth grant lives entirely in Vault, keyed per tenant — there is no
  shared fallback, unlike `CALCOM_API_KEY`.** `resolve_secret`'s "vault error is never
  absent" rule (above) still applies, but `app/mcp/oauth.py::_load_credentials` has no
  `env_value` to fall back to even on a genuine "no secret" result, because a shared
  refresh token would mean every `mcp_calcom` tenant authorizes into the *same* Cal.com
  account — the one design this layer exists to avoid. A tenant with no grant gets a
  `CalcomOAuthError` naming the exact `authorize_calcom` command to fix it, every time,
  not a silent cross-tenant leak.
- **`McpBookingProvider`'s MCP session cache is module-level, like `shared_async_client`'s
  client cache — not per-instance.** `get_booking_provider()` constructs a fresh
  `McpBookingProvider()` on every tool call (same as `CalcomBookingProvider`), so caching
  on `self` would cache nothing across calls. `app/tools/booking/mcp_calcom.py`'s
  `_session_cache` dict is module-global for exactly this reason — don't "clean up" it
  into an instance attribute.
- **`build_connection`'s `None`-means-skip-and-warn contract had to survive gaining an
  `auth: "oauth"` branch, because two callers need opposite behaviour from the same
  failure.** The long-tail tenant path (`app/mcp/client.py::_build_connections`) must
  degrade a bad server silently; the booking-critical path
  (`app/tools/booking/mcp_calcom.py::_connection_for`) must fail loudly as a
  `BookingError`. Rather than making `build_connection` raise for one caller and not the
  other, `_build_oauth_connection` keeps the existing never-raises contract, and
  `_connection_for` is the one that turns a `None` back into a `BookingError` — one
  implementation, two callers, each gets the failure mode it needs.
- **A tool argument shape this codebase could not verify offline, and had to be checked
  against a real account: `get_availability` / `create_booking` on Cal.com's hosted MCP
  server.** Cal.com's docs name the tools, not their parameters. Confirmed live
  2026-08-01 (plan §9 live check 3) — the guessed field names (matching what the proven
  REST provider already sends Cal.com's plain v2 API) turned out to be correct: a real
  availability query and a real booking both succeeded, and the booking matched an
  equivalent `"calcom"`-provider one when compared via Cal.com's own `/v2/bookings` API.
  `cancel_booking` / `reschedule_booking` are still unverified the same way — nothing
  calls them yet (`plans/phase10.md` item 5) — so don't assume they're equally safe.
  Don't read the offline tests alone as proof either way for any of the four; they prove
  the *provider's* logic (event-type resolution, error mapping, session caching), never
  Cal.com's actual schema — only a live call can do that, which is why this class of gap
  needed a real authorized grant to close, not just more unit tests.
- **`app/main.py`'s `lifespan` needed a second shutdown hook, not just
  `close_shared_clients()`.** Found live while latency-testing `McpBookingProvider`
  outside the request cycle: its per-tenant session cache
  (`app/tools/booking/mcp_calcom.py`) holds a live `streamablehttp_client` +
  `ClientSession` pair open indefinitely — that's the whole point of the cache — and
  nothing closed it on shutdown. Left alone, the interpreter tears the still-open async
  generators down at GC/exit time instead, from *outside* the task that opened them —
  observed live as "attempted to exit cancel scope in a different task" / "generator is
  already running" noise. Harmless in that nothing corrupted, but a real resource leak on
  every graceful shutdown, not cosmetic. Fixed by `aclose_calcom_mcp_sessions()`, called
  in `lifespan` right after `close_shared_clients()` — same pattern, second cache.
