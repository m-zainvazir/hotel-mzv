# AI Receptionist

One LangGraph brain, two channels (phone + chat), many tenants. Full spec in
[`AI-Receptionist-Build-Plan.md`](AI-Receptionist-Build-Plan.md); conventions in
[`CLAUDE.md`](CLAUDE.md).

**Status: Phases 1–7 complete and deployed; Phase 8 (analytics + per-tenant
admin) code-complete, pending live migration/deploy.** Live on Railway
(`us-east4`, Docker, one replica), backed by a Supabase project in
`us-east-1`. The brain runs on Groq/Gemini with the five
native tools wired, and is reachable by typed chat, an embeddable chat
widget, *and* by voice through Vapi's Custom-LLM mode. `hotel-mzv` books real
appointments against a live Cal.com calendar. Supabase now backs storage
(jobs/calls/messages/chat transcripts/escalations), real Row-Level Security,
per-tenant secrets in Vault, and a durable Postgres checkpointer — all
live-verified against a real project (see `plans/phase4.md`). The chat
widget's own `/chat/session` handshake, event filtering and transcript
persistence are live-verified too (see `plans/phase5.md`) — the two new
tables it needs (`chat_sessions`/`chat_messages`, `0006_chat.sql`) still need
applying to a live project the same manual way every prior migration did.
A tenant can now connect **any number of MCP servers** — a CRM, a search
tool, an internal API — and the brain uses their tools in conversation
alongside the five native ones (see `plans/phase6.md`); a first-party demo
server (`scripts/demo_mcp_server.py`) proves the whole path with zero
accounts. SMS (Twilio) and the Vapi warm transfer it would trigger are
implemented and tested but intentionally left off — parked by client
decision, not a technical gap. `northside-plumbing` stays on the in-process
booking logic (durable once Supabase is the store, just no external calendar
sync) as the second example tenant. Flipping any tenant's provider is a
one-line JSON edit either way — see `content/README.md`. An admin dashboard
at `/admin` now gives an operator per-tenant analytics and a real config
editor (see "Admin dashboard" below and `plans/phase8.md`) — code-complete
and fully tested offline, not yet live-verified (its own two new migrations
aren't applied to the live project yet, same "next manual step" every prior
phase has left behind).

## Talk to it — five doors, one brain

All five drive the same graph, so a booking made in any of them is the same
`jobs` row and the same confirmation.

| Door | Command | Needs |
|---|---|---|
| Terminal | `python -m scripts.chat_cli` | nothing but a Groq key |
| HTTP chat | `POST /chat` (SSE) | the dev server |
| **Chat widget** (browser) | `GET /widget/demo`, or embed `widget/dist/widget.js` on any page | the dev server + `npm --prefix widget run build` (see `widget/README.md`) |
| **Web call** (browser) | Vapi dashboard → "Talk to assistant" | tunnel + provisioned assistant |
| **Phone call** | dial the attached number | the above, plus `--attach-number` |

The web call and the phone call are the *same* Vapi assistant hitting the same
endpoint — attaching a number is what adds PSTN. Switching between them is
provisioning config, never a code change. Web calls skip the telephony leg, so
they're the cheaper way to iterate. The chat widget is cheaper still — no
telephony, no Vapi platform fee, just LLM tokens.

## Quick start

```bash
python -m venv .venv
.venv/Scripts/activate          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env            # add your GROQ_API_KEY
python -m scripts.chat_cli      # talk to it
```

Try: *"do you have a room available tonight?"* → it offers real slots,
collects your details, books the job and "texts" a confirmation. `/jobs`
shows what was booked, `/quit` exits.

```bash
python -m scripts.chat_cli --tenant northside-plumbing   # a different trade
python -m scripts.chat_cli --channel voice --show-tools  # voice-style replies
```

## Bringing up voice (Phase 2)

```bash
# 1. run the service
uvicorn app.main:app --reload
# Windows: if your org's Device Guard blocks .venv/Scripts/uvicorn.exe
# ("blocked by your organization's Device Guard policy"), run it through
# python instead — same server, no separate .exe to get flagged:
#   python -m uvicorn app.main:app --reload

# 2. expose it — copy the https URL into PUBLIC_BASE_URL in .env
ngrok http 8000

# 3. point a Vapi assistant at it (re-run whenever the tunnel URL changes)
python -m scripts.provision_vapi --tenant hotel-mzv --dry-run   # inspect first
python -m scripts.provision_vapi --tenant hotel-mzv

# 4. talk to it in the browser from the Vapi dashboard, then go live on a number
python -m scripts.provision_vapi --tenant hotel-mzv --attach-number +15551230000
python -m scripts.provision_vapi --tenant hotel-mzv --detach-number   # back to web only
python -m scripts.provision_vapi --tenant hotel-mzv --show
```

You need `VAPI_PRIVATE_KEY`, `PUBLIC_BASE_URL` and a Cartesia voice
(`CARTESIA_DEFAULT_VOICE_ID`, or `voice.voice_id` on the tenant). Set
`VAPI_WEBHOOK_SECRET` too — without it the voice endpoints are unauthenticated.

`--attach-number` imports your Twilio number when `TWILIO_ACCOUNT_SID` /
`TWILIO_AUTH_TOKEN` are set, and otherwise asks Vapi for one.

**Changing the voice** is one field in `content/tenants/<tenant>.json` plus a
re-run of `provision_vapi.py` — `voice.voice_id` for a different voice,
`voice.provider` for a different TTS vendor entirely. No code, no redeploy.
Phase 4's cloned voice drops into the same field.

## Commands

| | |
|---|---|
| Dev server | `uvicorn app.main:app --reload` (health: `GET /health`, reports `store`/`checkpointer`/`widget`/`admin`/`mcp`) — or `python -m uvicorn app.main:app --reload` if Windows Device Guard blocks the `.exe` |
| Terminal chat | `python -m scripts.chat_cli` |
| Build the widget | `npm --prefix widget install && npm --prefix widget run build` |
| Build the admin dashboard | `npm --prefix admin install && npm --prefix admin run build` (needs `ADMIN_ENABLED=true` + `ADMIN_AUTH_TOKEN` to actually use `/admin` — see `admin/README.md`) |
| Provision voice | `python -m scripts.provision_vapi --tenant <id> [--show\|--dry-run]` |
| Sync tenant → Supabase | `python -m scripts.sync_tenants [--tenant <id>] [--force\|--export]` |
| Onboard a tenant | `python -m scripts.onboard_tenant --config <file.json> [--dry-run]` |
| Run the demo MCP server | `pip install -e ".[mcp]"` then `python -m scripts.demo_mcp_server --port 8765` |
| Connect an MCP server to a tenant | `python -m scripts.register_mcp_server --tenant <id> --name <n> --url <url> [--secret <key>] [--list\|--disable\|--remove\|--dry-run]` |
| LangGraph Studio | `pip install -e ".[studio]"` then `langgraph dev` (see `langgraph.json`) |
| Load/latency test | `python -m scripts.loadtest --base-url <url> --endpoint chat\|voice --concurrency <n> --turns <n> --scenario question\|booking\|emergency` (see `infra/README.md`) |
| Build the deploy image | `docker build -f infra/Dockerfile .` (see `infra/README.md`) |
| Tests | `pytest` |
| Lint / format | `ruff check .` · `ruff format .` |

The test suite runs without any API key — a scripted chat model stands in for
Groq and streams token-by-token, so the streaming path under test is the real one.

## Layout

```
app/
  brain/        graph.py · runner.py (streaming) · nodes/ · prompts/ · sanitize.py
  channels/     chat.py (/chat/session + /chat SSE) · widget_auth.py (session tokens)
                vapi_llm.py (/chat/completions shim) · webhooks.py
                openai_compat.py · vapi_schema.py · vapi_provisioning.py
  tools/        native tier-1 tools + booking/ and messaging/ provider interfaces
  mcp/          per-tenant MCP registry, connections + loader (Phase 6)
  tenancy/      models · repository · cached loader · secrets.py · sync.py · voice.py
  db/           models · store protocols · memory_store · supabase_store · factory ·
                auth.py (RLS JWT) · checkpointer.py (durable Postgres) · migrations/
  main.py       the one service — CORS, /widget.js, /widget/demo, /health, /admin
scripts/  widget/  admin/  infra/  tests/
```

`widget/` is the embeddable chat widget's own source (Preact + TS, bundled
with Vite into a single `<script>` file) — see `widget/README.md`. `admin/`
(Phase 8) is the admin dashboard's source — same stack, but a plain SPA
build rather than an embed contract, since nothing pastes a `<script>` tag
pointing at it — see `admin/README.md`.

## How a turn flows

```
START → resolve_tenant → emergency_check → reason ⇄ tools → END
```

`resolve_tenant` maps a phone number / widget key / explicit id to a tenant and
loads its cached profile. `emergency_check` runs a deterministic, per-trade
keyword classifier — no LLM hop on the safety path. `reason` streams Groq tokens
with the tenant's tools bound — the five native ones plus, if the tenant has
any configured, MCP tools from its own servers (Phase 6). `tools` resolves
that same per-tenant set fresh on every invocation (`app/brain/nodes/tools.py`)
and executes whichever was called, then loops back.

Channels never contain logic: they call `stream_turn()` and re-encode its events
for their transport.

## Conventions that are load-bearing

* **Stream everything.** `reason` uses `astream`, never `ainvoke`. If the model
  calls a slow tool without speaking first, the runner injects a spoken
  acknowledgement so the caller never hears dead air.
* **Two tool tiers.** Critical path (`check_availability`, `book_job`,
  `send_confirmation`, `escalate`, `is_emergency`) is native and typed. Long-tail
  integrations go through MCP (`app/mcp/`, Phase 6) — a tenant connects any number
  of HTTP MCP servers via its own JSON or, in production, one row in the
  `mcp_servers` table (`scripts/register_mcp_server.py`, no redeploy needed).
  `app/brain/nodes/tools.py` resolves the same native-plus-MCP set `reason`
  bound and rebuilds `ToolNode` from it on every invocation — it can't be a
  static list once tools vary per tenant.
* **Tenant isolation.** Tools take `tenant_id` from the `RunnableConfig`, never
  from a model argument, so the LLM cannot cross tenants. Conversation threads
  are tenant-prefixed. `tests/test_tenant_isolation.py` guards this.
* **Provider-agnostic.** No graph node names Vapi, Twilio, Google or Groq.
* **Only spoken events become audio.** `tool_start` / `tool_result` carry raw
  tool output; leak them into the voice stream and the caller hears
  "slot_start_iso equals two thousand twenty six dash".
* **Never raise mid-stream on voice.** An HTTP error after the first chunk is
  silence on a live call. Errors become a spoken apology and a clean stop.
* **Tenancy never comes from the request body — on any channel.** Voice reads
  the Vapi assistant id and the dialled number; the chat widget reads a
  verified session token minted by `POST /chat/session`, never a `tenant_id`
  a caller could set. `app/channels/chat.py`'s `require_chat_caller` is what
  enforces this on `/chat`.
* **Cal.com, not the tenant JSON, owns availability once a tenant is on
  `"calcom"`.** `booking.hours`/`lead_time_hours`/`slot_granularity_minutes`
  become prompt copy only — the real schedule lives on the Cal.com event type.
* **A tool signals structured data with an artifact, never by asking the
  model to emit markup.** `escalate` returns `(text, {"kind": "handoff",
  ...})`; `check_availability` returns `(text, {"kind": "slots", ...})`. The
  runner turns these into `BrainEvent("handoff")` / `BrainEvent("suggestions")`
  — only `app/channels/vapi_llm.py` knows what a Vapi `transferCall` frame
  looks like, and only the widget knows what a quick-reply chip is.
* **The chat widget's `<script>` tag is a frozen contract.** Once a client
  site has it pasted in, the tag itself (its attributes, and the
  `/chat/session` → `/chat` protocol behind it) can't change — only the
  bundle behind it can. See `widget/README.md`.

## Cost: requests, not turns

The provider bills and rate-limits **requests**, and one conversational turn is
rarely one request:

```
1 turn  =  1 request
        +  1 per tool hop      (check_availability → book_job → send_confirmation = 3 more)
        +  1 per retry         (a malformed tool call costs a second attempt)
```

A four-turn booking conversation is therefore ~10–15 requests, not 4. On top of
that each request re-sends a fixed overhead:

| Per request | ~tokens |
|---|---|
| System prompt (services, hours, rules) | ~670 |
| Tool schemas (5 native tools) | ~790 |
| **Fixed floor before any conversation** | **~1,460** |

So ~76 requests ≈ 100k tokens — which is exactly Groq's free-tier daily cap.
`GET /health` and the `turn used N llm requests` log line make this visible.

**MCP tools (Phase 6) add to the floor above, on every turn a tenant has any
configured** — every bound tool schema is re-sent on every request, whether
or not that turn needs it. `MCP_MAX_TOOLS` (default 8) caps the damage;
`tool_allowlist` on a tenant's own server config is the finer-grained knob.

Levers, cheapest first: switch to a smaller/cheaper model (one env var, see
`.env.example`), trim the system prompt, drop tools the graph doesn't need bound
every turn, narrow a tenant's `tool_allowlist` or `MCP_MAX_TOOLS`, or fold
`send_confirmation` into `book_job` to remove a hop.

## Groq tool-calling, in practice

Everything here was observed against the live API, not anticipated:

* Llama 3.3 sometimes emits a tool call as plain text
  (`<function=...>{...}</function>`) — stripped from speech and promoted to a
  real call (`app/brain/sanitize.py`).
* It sometimes emits one malformed enough that Groq rejects the whole request —
  retried once with tools withheld, so the caller gets a normal spoken turn
  instead of dead air (`app/brain/nodes/reason.py`).
* After a tool returns it tends to **restate its own acknowledgement**, usually
  truncated ("Let me check what we've got available for you." → "Let me check
  what we've got…"). Unnoticeable in text, plainly odd aloud, so the repeat is
  suppressed deterministically (`RepeatSuppressor`) rather than left to prompt
  wording.

Together these are the plan §17 risk, mitigated rather than theorised about.

## What's still stubbed

`hotel-mzv` books against a real Cal.com calendar (`CalcomBookingProvider`).
`TwilioNotifier` and `WarmTransferEscalator` are implemented and tested
against mocks, but no tenant has `notifications.provider: "twilio"` — SMS
confirmations and alerts are parked by client decision, not a technical gap.
`northside-plumbing` stays on the built-in booking logic (`"stub"` provider —
not fake once Supabase is the store, just no external calendar sync) as the
second example tenant.

| Real | Stubbed |
|---|---|
| Scheduling: real Cal.com calendar for hotel-mzv; hours/lead-time/conflicts logic (durable, DB-backed once Supabase is the store) for any tenant on `"stub"` (northside-plumbing) | The calendar *itself*, for tenants on `"stub"` |
| Emergency detection; Vapi warm transfer (voice, on by default once a tenant has an `escalation_phone`) | The SMS alert/confirmation leg — no tenant has `notifications.provider: "twilio"` yet |
| Storage: jobs/calls/messages/escalations in Supabase Postgres with real RLS, per-tenant secrets in Vault, a durable checkpointer | Tenant *config* still reads from JSON files **by default** — `TENANT_SOURCE=supabase` (Phase 8) makes the flip real, required once `ADMIN_ENABLED=true`, but not yet live-verified against the real project |
| Voice: streaming, tenancy, call records, provisioning; consent enforcement (Python + a DB trigger) | The actual voice clone — no tenant has a real cloned `voice_id` yet, needs an audio sample + written consent |
| Per-tenant Cal.com/Twilio credentials via Vault, live-verified | A second real Cal.com account, to prove two tenants booking into two different calendars end-to-end (currently only credential *resolution* is proven, not a full live booking) |
| Chat widget: handshake, streaming, quick replies, event filtering, CORS — live-verified end to end against real Gemini + Cal.com | `chat_sessions`/`chat_messages` (`app/db/migrations/0006_chat.sql`) — written and offline-tested, but not yet applied to a live project; until then transcript writes fail (caught, logged, never break the stream) and every conversation lives only in the checkpointer |
| MCP: any tenant can connect HTTP MCP servers (registry, secret substitution, per-server timeouts, tenant-scoped caching) — proven against a first-party demo server (`scripts/demo_mcp_server.py`) | A concrete third-party search/scraper server (Tavily/Firecrawl/Exa) — the config path is generic and vendor-neutral, just needs a key; `mcp_servers` (`app/db/migrations/0007_mcp.sql`) not yet applied to a live project either |

See `plans/phase4.md`, `plans/phase5.md` and `plans/phase6.md` for the full
implementation records and live-verification checklists.

## Deploy (Phase 7) — done

`plans/phase7.md` is the full plan; `infra/README.md` is the runbook. Live
on Railway: Docker builder (`railway.json` → `infra/Dockerfile`), region
`us-east4`, one replica. Supabase re-created in `us-east-1` (was Asia) —
Vapi and the active LLM provider are both US-anchored with no region
selector of their own, so co-locating the app + DB there is the one real
lever available; all 7 migrations applied, both tenants synced, per-tenant
Vault secrets restored. `provision_vapi` re-run against the live URL, in
the required order (domain → secrets → provision → commit → deploy).
Confirmed live: `/health` (`checkpointer: "postgres"`, `store: "supabase"`,
`problems: []`), `/readyz`, the deployed webhook secret matching the
provisioned Vapi assistant, and a real turn against the live assistant id
returning a correct answer from the real model. hotel-mzv's warm transfer
is off for now (its escalation number was placeholder `555` data Vapi's
API rejects as a real destination — one JSON edit + a `provision_vapi`
re-run once a real number exists). No phone number attached yet (web-call
only) and Twilio stays parked — both are the client's call.

## Admin dashboard (Phase 8) — code-complete

`plans/phase8.md` is the full plan. An operator surface at `/admin` —
per-tenant analytics (calls, chat volume, bookings, escalations, cost — see
the caveat below) and a real config editor, replacing "edit
`content/tenants/*.json`, commit, redeploy" with a form that takes effect on
the very next turn. Same-origin, no CORS involved, served from the one app
(`app/main.py`'s guarded `StaticFiles` mount + SPA catch-all).

**Fails closed, not open — the one deliberate exception in this codebase's
security posture.** `ADMIN_ENABLED=false` by default (every route 404s);
`ADMIN_AUTH_TOKEN` unset means every request 401s, unlike every other secret
in this app. `app/preflight.py` refuses `ADMIN_ENABLED=true` in production
without a real `ADMIN_AUTH_TOKEN` (32+ chars) and without `TENANT_SOURCE=supabase`
— the latter isn't a style preference: an admin panel editing config while
the app still reads `content/tenants/*.json` produces edits that reach
Postgres and never reach the bot ("the phantom edit" — see `plans/phase8.md`).

**Built "operator-only now, designed for tenant login later"**: an
`AdminPrincipal` abstraction, the tenant id always in the URL path, and
every analytics read already going through the same tenant-scoped JWT a
future logged-in tenant's own reads would use — so that flip
(`plans/phase10.md` item 14) is additive, not a rewrite.

**Nothing here touches the brain.** It's a new read/write surface over
tenant config and existing storage — `app/brain/` is unmodified.

Not yet live-verified: `0008_analytics.sql` and `0009_admin.sql` aren't
applied to the live Supabase project, so `/admin/api/overview`'s per-tenant
metrics currently degrade cleanly (a per-tenant "failed to load", proven
against the real project, not just offline) until that migration step
happens — same "apply the SQL by hand" step every prior phase has needed.
There's also no per-tenant LLM cost or per-turn latency anywhere in this
app (`app/brain/metrics.py` is process-global by design), so the dashboard
doesn't show either — the "Vapi telephony cost" tile is exactly that, not a
total cost figure.

## Security

`.env`, `apis.md` and credential files are gitignored. Secrets belong in env
vars / Supabase Vault, never in code. Voice cloning requires stored written
consent — no exceptions.

One admin note, one chat-widget note, then two voice-specific ones:

* **`ADMIN_AUTH_TOKEN` has the largest blast radius of any secret in this
  app** — full read of every call/chat transcript, full write of every
  tenant's config, including `emergency.escalation_phone` (redirects
  emergencies) and `booking.event_type_id` (redirects bookings to a
  different calendar). Never reuse `API_AUTH_TOKEN` for it — that token's
  power is "run a conversation as any tenant", and conflating the two would
  silently promote every existing holder (dev boxes, `chat_cli`, tests, CI)
  to full admin. Rotate it, keep the holder list short, and set
  `ADMIN_ENABLED=false` on any box that doesn't need the surface at all.

* **The widget session token (`app/channels/widget_auth.py`) is the real
  tenancy boundary for `/chat`**, not `API_AUTH_TOKEN` (a browser can't hold a
  shared secret). Set `WIDGET_SESSION_SECRET` in production so tokens survive
  a redeploy — unset, a random per-process key is generated instead, which
  works fine but invalidates every open widget session on restart. CORS on
  `/chat`/`/chat/session` is deliberately permissive (`allow_origins="*"`,
  `allow_credentials=False`); the actual per-tenant boundary is
  `chat.allowed_origins`, checked once at the handshake — see
  `content/README.md`.

* Set `VAPI_WEBHOOK_SECRET`. Without it `/chat/completions` and `/webhooks/vapi`
  accept anything, and both spend money. We verify either the `x-vapi-secret`
  header or an HMAC `x-vapi-signature`, since Vapi sends one or the other
  depending on how the assistant's `server` block is configured.
* **Vapi puts the tenant's Twilio credentials in the request body**
  (`phoneNumber.twilioAccountSid` / `twilioAuthToken`). Anything that logs or
  stores a payload must go through `vapi_schema.redacted()` first — the
  committed test fixture is redacted for the same reason.
* **`CALCOM_API_KEY` / `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` in `.env` are
  the shared fallback, not the source of truth.** A tenant with its own
  credentials in Vault (`scripts.onboard_tenant --calcom-api-key ...`) uses
  those instead — see `app/tenancy/secrets.py`. Notifiers log
  tenant/kind/message-sid/status — never the SMS body (it carries a guest's
  name and address).
* **Row-Level Security is real**, not defence-in-depth theatre: every
  Supabase table is `FORCE ROW LEVEL SECURITY` plus a `tenant_id`-scoped
  policy, and the backend mints a short-lived per-tenant JWT
  (`app/db/auth.py`) for every request rather than using one shared
  credential. The Supabase **secret** key bypasses RLS entirely and is used
  only for admin/onboarding paths — never for a request scoped to one
  tenant.
