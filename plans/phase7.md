# Phase 7 — Deploy + harden

## Context

Phases 1–6 are done. The brain answers voice and chat, books a real Cal.com
calendar, persists to Supabase behind real RLS, ships an embeddable widget, and
loads per-tenant MCP tools. Every one of those was verified *on this Windows dev
box against real services* — and that is exactly the problem Phase 7 exists to
fix. Nothing has ever run anywhere else.

Plan §15's criterion is *"it's live on real numbers and hits the latency target
under load."* Reaching it means closing gaps that are invisible in development
and load-bearing in production. Six are confirmed in the code, not anticipated:

1. **`infra/Dockerfile:22` runs `pip install .` with no extras.** The deployed
   image therefore cannot use the Postgres checkpointer (silently degrades to
   `InMemorySaver` per `app/brain/graph.py:89-95` — one WARNING line), cannot
   use `LLM_PROVIDER=google|openai`, and cannot load MCP tools. Phase 4's entire
   durability workstream is absent from the artifact that would ship.
2. **Both auth guards fail open, and nothing ties that to `APP_ENV`.**
   `app/channels/security.py:61-62` — with `API_AUTH_TOKEN` unset, every
   anonymous caller to `POST /chat` is granted `mode="trusted"`, which is
   precisely the mode that reads `tenant_id` from the request body. `:85-86` —
   with `VAPI_WEBHOOK_SECRET` unset, `/chat/completions` and `/webhooks/vapi`
   are unauthenticated. The only production hard-fail in the codebase is
   `SUPABASE_URL` (`app/db/factory.py:40-45`).
3. **LangSmith tracing, which plan §15 names as a Phase 7 deliverable, is
   inert.** `.env.example:227-229` sets `LANGCHAIN_TRACING_V2` etc., but they are
   not `Settings` fields, `Settings.model_config` has `extra="ignore"`
   (`app/config.py:20-25`), and nothing calls `load_dotenv()` anywhere in the
   repo. pydantic-settings reads `.env` without exporting to `os.environ`, and
   LangChain's tracer reads `os.environ` — so the documented switch does nothing
   under uvicorn. It happens to work under `langgraph dev`, which loads `.env`
   itself (`langgraph.json`), which is why this was never noticed.
4. **Nothing measures latency under load.** `FIRST_TOKEN_BUDGET_MS = 400.0`
   (`app/channels/vapi_llm.py:46`) is the only timer in the codebase, it exists
   on the voice streaming path only, `POST /chat` is entirely uninstrumented, and
   `TurnCounter` (`app/brain/metrics.py:42-56`) is a process-global difference
   that reports wrong numbers the moment two turns overlap.
5. **No rate limiting of any kind.** Groq's free tier is ~76 requests/day
   (README's cost section) and anyone holding a public widget key can spend it,
   book real Cal.com appointments, and trigger escalations.
6. **The repo has one commit and all of Phase 6 is uncommitted** — 18 modified,
   12 untracked, including `app/brain/nodes/tools.py`, `app/mcp/*` and
   `0007_mcp.sql`. Railway deploys from git, so a deploy today ships Phase 5.

Outcome: the one service running always-on in a US region, with secrets that
cannot be silently absent, logs you can correlate, a measured latency number
against §13, and a live phone call proving it.

## Decisions locked

| | |
|---|---|
| **Host** | **Railway**, Docker builder, `dockerfilePath: infra/Dockerfile`. Always-on by default — no scale-to-zero, which `plans/phase4.md:126` records as fatal for inbound calls (Vapi times out while a container wakes and the caller hears nothing). |
| **App region** | **`us-east4`.** Non-negotiable given Groq and Vapi are US-based: the first-token path is Vapi → us → Groq → back, and putting the app in Asia adds ~400ms of pure RTT to a 600–800ms end-to-end budget. |
| **Supabase region** | **Recommend re-creating the project in `us-east-1`** — see "The region trade-off". Fallback documented if you'd rather not. |
| **Process model** | **One worker, one replica.** `app/brain/metrics.py`'s counters, `app/channels/widget_auth.py:33`'s fallback secret, and the new rate limiter are all per-process. `--workers > 1` or a second replica breaks all three silently. Stated in the Dockerfile and CLAUDE.md, not left to be rediscovered. |
| **Rate limiting** | **In-process, per-IP + per-session + per-tenant, no new dependency.** A ceiling, not the usage-tier model `plans/phase10.md` defers. Applied to `/chat` and `/chat/session` only — never to `/chat/completions` or `/webhooks/vapi`, which are secret-gated and where throttling a live call is worse than the spend. |
| **SMS** | Sender is **non-US, so no A2P 10DLC.** Twilio go-live rides along in Step 11 once you name the country — but see the caveat in "What I need from you"; "not US" does not always mean "no registration". |
| **Lockfile** | **Generated inside the Linux image, committed.** No lockfile exists today and every dep is a `>=` floor. CLAUDE.md already records two separate incidents of pip silently moving this stack. Generating it on Windows would bake in `pywin32` and the win32-only `uuid-utils` cap. |

### The region trade-off, honestly

Your Supabase project is in Asia; Groq, Vapi and therefore the app must be in the
US. That split is not free, and it is worse than it first looks:

`app/brain/runner.py:201`'s `thread_is_cold` check runs a **checkpointer state
read before the graph streams anything**, on every voice turn. With the app in
`us-east4` and Postgres in Singapore that is ~230ms of RTT added to first-token
latency *per turn*, on top of the booking-path round trips. Worse, it only
appears once the Postgres checkpointer is actually installed — which Step 2 does
for the first time. So it would arrive looking like a regression Step 2 caused.

Three options, in order of preference:

1. **Re-create the Supabase project in `us-east-1`.** Cheaper than it sounds and
   it retires existing debt: `0006_chat.sql` and `0007_mcp.sql` are unapplied
   anyway (CLAUDE.md "Next"), so instead of applying two migrations by hand you
   apply all seven to a fresh project. Everything else is scripted —
   `sync_tenants.py` pushes tenant rows, `onboard_tenant.py --calcom-api-key`
   re-stores Vault secrets. The only loss is existing `jobs`/`calls` rows, which
   are development test data.
2. **Keep Asia, deploy US anyway**, measure the real cost in Step 9, and accept
   it. Bookings get slower; first token gets slower by the checkpointer read.
3. **Keep Asia and disable the durable checkpointer** to protect first-token
   latency. Rejected — it throws away Phase 4's durability to fix a placement
   problem, and web chat loses conversation state on every redeploy.

I've planned for (1) and Step 10 falls back to (2) cleanly if you decline.

## What I need from you

**Nothing blocks Steps 0–9.** All of it is code, tests and config.

1. **The sending country for SMS** (Step 11). "Non-US" removes A2P 10DLC, but it
   does not always remove registration: India mandates DLT, and Twilio requires
   pre-registered alphanumeric sender IDs for a number of APAC/Middle-East
   destinations. Tell me the country and I'll confirm what actually applies
   before flipping `notifications.provider` to `"twilio"`.
2. **One verified handset number** to send a real confirmation to (Twilio trial
   accounts can only text verified numbers).
3. **A go/no-go on re-creating Supabase in `us-east-1`** (Step 10).
4. **A LangSmith API key** (Step 7) — free dev tier, `LANGCHAIN_API_KEY`.
   Non-blocking: tracing stays off without it.
5. **Optional: a custom domain.** `PUBLIC_BASE_URL` is baked into the Vapi
   assistant at provisioning time and never re-read at runtime, so every URL
   change costs a `provision_vapi` re-run per tenant. A custom domain means you
   pay that once, ever. A Railway subdomain is fine to start.

---

## Implementation

Each step ends with a green `pytest` and `ruff check .`.

### Step 0 — baseline and commit the tree

`pytest` → record the count (~357 test functions, more after parametrisation).
`ruff check .`. Confirm `widget/dist/.buildhash` matches
(`tests/test_widget_bundle.py` — note it **skips silently** when the bundle is
absent, so a green suite is not proof the widget is built).

Then **commit Phase 6.** Railway builds from git; an uncommitted
`app/brain/nodes/tools.py` means deploying the static-`ToolNode` bug Phase 6
fixed. Add `graphify-out/` to `.gitignore` (build output of a local tool, not
project content) and decide whether `chats/` belongs in the repo.

### Step 1 — pin the dependency set

Add `infra/requirements.lock.txt`, generated **inside a Linux container** so it
carries no win32 artefacts:

```
docker build -f infra/Dockerfile --target deps -t air-deps .
docker run --rm air-deps pip freeze > infra/requirements.lock.txt
```

The runtime install becomes `pip install -r infra/requirements.lock.txt` followed
by `pip install --no-deps .`. This is the mitigation for a failure mode this
project has already hit twice (CLAUDE.md's `uuid_utils` and `[google]` gotchas):
two builds of the same commit currently resolve different versions.

### Step 2 — harden `infra/Dockerfile`

- **Install the extras production needs:** `.[postgres]` at minimum, plus
  `[mcp]` and whichever of `[google]`/`[openai]` matches `LLM_PROVIDER`. The
  `[google]` extra's `uuid-utils<0.16` cap is `sys_platform == "win32"`, so it is
  inert on Linux.
- **Honour `$PORT`** — Railway injects it and `app/config.py`'s `host`/`port`
  fields are read nowhere. Use `exec` inside `sh -c` so signals still forward:
  ```
  CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} \
       --proxy-headers --forwarded-allow-ips='*' \
       --timeout-graceful-shutdown 25 --timeout-keep-alive 75"]
  ```
  `--proxy-headers` is load-bearing twice: without it every client IP is
  Railway's proxy (defeating Step 6's limiter) and the scheme is wrong.
  `--timeout-graceful-shutdown` gives SSE streams and live voice turns a drain
  deadline — today a redeploy mid-call has none.
- **Split layers** — copy `pyproject.toml` + the lock, install, *then* copy
  `app/ scripts/ content/ widget/dist`. Today every code edit reinstalls the
  whole dependency tree.
- Add a non-root `USER`, a `HEALTHCHECK` on `/health`, and a root
  **`.dockerignore`** (the build context currently ships `.venv/`,
  `widget/node_modules/`, `graphify-out/` and `.git/` to the daemon).
- Comment the **single-worker constraint** and why.

Remember the Dockerfile lives in `infra/` with root-relative `COPY` paths — it
must build from the repo root with `-f infra/Dockerfile .`.

### Step 3 — production preflight (`app/preflight.py`, new)

`verify_production_settings(settings) -> list[str]`, called from `lifespan`
(`app/main.py:82-99`) before `get_store()`, raising **one** aggregated error
listing every problem rather than failing one at a time. When
`app_env == "production"`, all of these are fatal:

- `api_auth_token` unset → `/chat` is open and body-driven (defect 2)
- `vapi_webhook_secret` unset → the voice endpoints are open
- `widget_session_secret` unset → tokens are per-process and die on redeploy
- `public_base_url` unset or not `https://`
- `supabase_jwt_secret` unset while `supabase_url` is set — today this raises
  `SupabaseAuthNotConfiguredError` mid-call on the first tenant-scoped query
- no API key for the active `llm_provider`

`database_url` unset stays a **WARNING**, not fatal — Phase 4 deliberately made
checkpointer degradation non-fatal, and Step 4 surfaces it in `/health` instead.
Leave `app/db/factory.py`'s existing `SUPABASE_URL` raise where it is.

### Step 4 — `/health` tells the truth, and `/readyz` exists

`app/main.py:155-172`. Three changes:

- **`status` can be `"degraded"`.** It is a hardcoded `"ok"` today, so it can
  never report a problem. Add a `problems: []` list covering: `checkpointer ==
  "memory"` while `database_url` is set (the *only* signal that a bad
  `DATABASE_URL` silently cost you durability), `widget == "missing"`, `mcp ==
  "unavailable"`.
- **Stop leaking detail to anonymous callers.** The public body drops `env`,
  `model`, `llm_provider` and the full `tenants` list; those move behind the
  `API_AUTH_TOKEN` bearer. `tests/test_api.py` asserts on `tenants`,
  `llm_provider` and `store` today and must be updated.
- **New `GET /readyz`** doing one cheap authenticated Supabase read. Keep it off
  `/health`, which Railway polls frequently and which would otherwise pay a
  US→DB round trip each time. This also fixes a latent hole in
  `plans/phase4.md:116`: the keep-alive was specified against `/health`, which
  touches no database, so it would never have stopped a free project pausing.

### Step 5 — structured, correlated logs (and two PII leaks)

`app/logging_config.py` gains a JSON formatter selected by a new
`log_format: Literal["text","json"] = "text"` setting (`json` in production).
Include the **date** — `datefmt="%H:%M:%S"` currently drops it, making logs
useless across a day boundary.

- Install handlers explicitly instead of `logging.basicConfig`, which is a no-op
  when a root handler already exists — a platform wrapper can silently void
  `LOG_LEVEL` today.
- Take over `uvicorn`, `uvicorn.access` and `uvicorn.error` so stdout carries one
  format, and call `configure_logging()` at import in `app/main.py` rather than
  inside `lifespan`, where it runs *after* uvicorn's own startup lines.
- **New `app/middleware.py`:** accept or generate `X-Request-Id`, put it in a
  `ContextVar` the formatter reads, echo it back, and log one access line with
  duration. This is also where `/chat`'s missing timing comes from.
- **PII:** `app/tools/messaging/stub.py:33` logs the full SMS body at INFO —
  guest name, appointment time and destination number — and it is the *live*
  path for every tenant not on Twilio. Drop to DEBUG. `app/brain/graph.py:90`
  logs a psycopg traceback with `exc_info=True`; some psycopg error paths embed
  the conninfo **including the password** — scrub `database_url` from that line.
  Leave the escalation-number logs (`messaging_tools.py:146-152`) alone: that is
  the business's own number and operationally necessary.

### Step 6 — rate limiting (`app/channels/ratelimit.py`, new)

Fixed-window counters in a dict behind an `RLock`, the same shape as
`app/tenancy/loader.py`'s TTL cache. No new dependency. Keys, most specific
first: widget session (`tid` + `sid` from the verified token), client IP (from
`X-Forwarded-For`, hence Step 2's `--proxy-headers`), tenant.

New settings: `rate_limit_enabled=True`, `chat_requests_per_minute=20`,
`chat_requests_per_day=200`, `session_requests_per_hour=30`. Returns **429 with
`Retry-After`** — the app returns no 429 anywhere today.

Applied as a dependency on `POST /chat` and `POST /chat/session` only. State the
limitation plainly: **per-replica**, therefore correct only at one replica, which
is what we deploy and what the single-worker constraint already requires. Shared
state is phase10 territory alongside the usage-tier model.

Whether a global concurrency ceiling is also needed is **deferred to Step 9's
measurement** — the psycopg pool is `max_size=3` (`app/db/checkpointer.py:64`)
and a thundering herd may starve it. Measure before adding.

### Step 7 — make LangSmith tracing real

Add `langchain_tracing_v2: bool = False`, `langchain_api_key`,
`langchain_project` as **real `Settings` fields** — keeping the env var names
already in `.env.example` so nothing churns — and export them into `os.environ`
in `lifespan` **before `get_graph()`**. LangChain's tracer reads `os.environ`;
that is the whole bug. `langsmith` is already a transitive dependency of
`langchain-core`, so this adds nothing to install.

Side benefit worth naming: as real fields they are now stripped by
`hermetic_settings` (`tests/conftest.py:147`). Today a developer with tracing on
would have the *test suite* posting transcripts to LangSmith — and the
`no_network` guard would not catch it, because it patches httpx transports while
`langsmith` uses `requests`.

Add `"tracing"` to the authenticated `/health` detail.

### Step 8 — CI and the keep-alive (`.github/`, new — nothing exists today)

- **`ci.yml`** — `ruff check .`, `ruff format --check .`, `pytest` on push and
  PR. Deliberately **no Node step**: `tests/test_widget_bundle.py` already
  catches `dist/`-vs-`src/` drift in Python, which is why Phase 5 chose that
  design. Add a `docker build -f infra/Dockerfile .` job so the image cannot rot.
- **`keepalive.yml`** — cron every ~3 days hitting `GET /readyz` (Step 4), which
  actually touches the database. Free Supabase projects pause after 7 idle days,
  and a paused project means the receptionist answers and cannot book. Needs one
  repo secret for the deploy URL.

### Step 9 — the load/latency harness (`scripts/loadtest.py`, new)

Stdlib + httpx, both already dependencies. Drives a **running** server:

- `POST /chat` (SSE) and `POST /chat/completions` using
  `tests/fixtures/vapi_chat_completion_request.json` as the Vapi-shaped template.
- Measures per turn: request → first `delta.content` (the number
  `FIRST_TOKEN_BUDGET_MS` guards), request → `final`, and tool-hop count.
- `--concurrency N --turns M --scenario {question,booking,emergency}`, reporting
  **p50/p95/p99** and pass/fail against §13.
- Add per-tool duration logging around `check_availability` / `book_job` so the
  report can attribute the budget — Cal.com's 15s timeout is the single largest
  unmeasured span in a turn.

**Measure LLM request cost at `--concurrency 1` only**, and document why:
`TurnCounter` is a global difference, so under overlap it counts other turns'
requests too. Rewriting `app/brain/metrics.py` is out of scope — its docstring
records that the obvious `ContextVar` fix already failed once because the model
call happens inside LangGraph's own task.

### Step 10 — Supabase: migrations, and the region

If re-creating in `us-east-1`: create the project, apply
`0001`→`0007` in order, `python -m scripts.sync_tenants`, re-store per-tenant
Vault secrets via `onboard_tenant --calcom-api-key`, then update `SUPABASE_*` and
`DATABASE_URL`.

If keeping Asia: apply just `0006_chat.sql` + `0007_mcp.sql` — the step CLAUDE.md
has been carrying as "Next" since Phase 5.

Either way, `DATABASE_URL` must be the **Supavisor session-mode pooler**,
`aws-0-<region>.pooler.supabase.com` on **port 5432** — never the direct
`db.<ref>.supabase.co` host (IPv6-only since Jan 2024) and never 6543
(transaction mode kills `AsyncPostgresSaver`'s prepared statements). Then verify
`checkpointer: "postgres"` in `/health` and that `/rest/v1/checkpoints` 404s —
the proof the `langgraph` schema actually hid transcripts from PostgREST.

### Step 11 — deploy, then re-provision Vapi (order matters)

Commit `railway.json`: Docker builder, `dockerfilePath: infra/Dockerfile`,
`healthcheckPath: /health`, one replica. Set the region to **us-east4** in the
service settings. Every secret is a platform variable — never baked into the
image. Set `APP_ENV=production`, which now trips Step 3's preflight, so this is
the moment every secret must be real.

**Sequence, because getting it wrong is very hard to debug from a phone:**

1. Reserve the Railway domain **first** and put it in `PUBLIC_BASE_URL`.
2. Run `python -m scripts.provision_vapi --tenant hotel-mzv` from the dev box.
3. Commit the `content/tenants/*.json` it writes back.
4. *Then* deploy.

`provision_vapi` writes `vapi.assistant_id` into the tenant JSON, and the image
bakes `content/`. Provision after deploying and the running image has a JSON
without the new assistant id, so `find_by_assistant_id` misses and tenant
resolution silently falls through to the dialled number or the default tenant.

Re-provisioning is mandatory regardless: `PUBLIC_BASE_URL` is read *only* at
provisioning time and baked into two different Vapi fields (`model.url` and
`server.url`), and `VAPI_WEBHOOK_SECRET` into both `model.headers` and
`server.secret`. Miss it and the caller hears the greeting — spoken by TTS with
no LLM round trip — and then silence, while every turn 401s and `/health` stays
green. This is also the run that finally fixes the escalation-number mismatch
CLAUDE.md already flags.

Then, given the country from "What I need from you": flip
`notifications.provider` to `"twilio"` and send one real confirmation.

### Step 12 — docs

`CLAUDE.md` (Phase 7 done; new gotchas: `$PORT`, the single-worker constraint,
the `LANGCHAIN_*`-never-reaches-`os.environ` trap, the preflight, the
provision-before-deploy ordering trap); `README.md` (status + a Deploy section);
`infra/README.md` — replace the 12-line stub with a real runbook; `.env.example`
(new fields); and an amendment note on plan §15/§13 recording the region
trade-off and its resolution.

---

## Testing

Offline, on the existing `ScriptedChatModel` + `mock_http` + autouse
`no_network` fixtures.

**New:**
- `tests/test_preflight.py` — parametrised: each missing secret fails the boot
  under `APP_ENV=production` and none fails under `development`; the error names
  *every* problem, not just the first.
- `tests/test_ratelimit.py` — window accounting against an injected clock; the
  429 carries `Retry-After`; a widget session is limited independently of
  another; `/chat/completions` and `/webhooks/vapi` are **never** limited.
- `tests/test_logging.py` — JSON records parse and carry `request_id`; the
  request id round-trips from an inbound header; the SMS body no longer appears
  at INFO; `database_url` never appears in the checkpointer failure log.
- `tests/test_deploy_config.py` — the same idea as `tests/test_migrations.py`'s
  lint, aimed at deploy config: the Dockerfile installs the `postgres` extra,
  honours `${PORT`, passes `--proxy-headers`, and `.dockerignore` excludes
  `.venv`/`node_modules`. This permanently closes "someone dropped the extras
  again", which is how the current gap arose.

**Updated:** `tests/test_api.py`'s `/health` assertions (Step 4 moves `tenants` /
`llm_provider` behind auth).

**Live verification, in order:**

1. `docker build -f infra/Dockerfile .` succeeds; the container boots and
   `/health` reports `checkpointer: "postgres"`, `store: "supabase"`,
   `widget: "built"`.
2. `APP_ENV=production` with each secret removed in turn **fails the boot** with
   a message naming it.
3. Railway deploy green; `/readyz` 200; region confirmed us-east4.
4. `provision_vapi --show` reflects the Railway URL, not ngrok.
5. **A real phone call books a real Cal.com appointment** — the Phase 7
   criterion. Hang up, confirm a `calls` row with transcript and duration.
6. `chat_sessions`/`chat_messages` rows finally appear (they 404 today —
   `0006_chat.sql`).
7. `scripts/loadtest.py --concurrency 1` then `5` and `10`: p50/p95 first-token
   against §13's budget, with the app↔DB gap called out separately.
8. Kill the demo MCP server mid-conversation on the deployed instance — the turn
   still completes, degraded.
9. Rate limiter returns 429 at the configured ceiling and normal use never trips
   it.
10. LangSmith shows traces for a real call.
11. Redeploy while an SSE stream is open — it drains rather than truncating.
12. Widget embedded from a genuinely different origin, with
    `chat.allowed_origins` set for real (Phase 5 deferred this as "a Phase 7
    concern").

## Risks

1. **The Asia↔US split** — the biggest one, and the reason Step 10 leads with
   re-creating the project. Its worst property is that the cost only appears
   once Step 2 installs the Postgres checkpointer, so it will look like Step 2
   caused it.
2. **`PUBLIC_BASE_URL` drift has no detection.** It is not read at runtime and
   not validated at boot, so a stale value is invisible from the app side while
   every call fails. Step 3 validates it at boot; nothing compares it to what
   Vapi actually has stored. Accept and document.
3. **Single-worker is a real constraint, not a default.** Railway autoscaling or
   an idle `--workers 2` silently breaks the rate limiter, the metrics counters
   and widget session tokens. Documented in three places on purpose.
4. **The in-process limiter is per-replica.** Correct at one replica; wrong the
   moment there are two, in the permissive direction.
5. **Tracing sends transcripts to a third party.** Chat and call transcripts
   carry guest names and phone numbers, so `LANGCHAIN_TRACING_V2=true` is a PII
   decision, not just an ops one. Default off; say so in `.env.example`.
6. **`pip freeze` inside Docker pins the lock to that build's resolution** —
   including anything that had already drifted. Diff it against the dev `.venv`
   for the packages CLAUDE.md names (`langchain-core`, `langgraph`) before
   trusting it.
7. **`0006`/`0007` are still hand-applied.** No migration runner exists and this
   phase does not add one; a forgotten migration is a caught-and-logged 404, not
   an error. Step 10's live checks are the only guard.
8. **Free Supabase still pauses.** Step 8's keep-alive mitigates it; it depends
   on a GitHub Actions cron actually firing, which is not guaranteed on a quiet
   repo. Check it after a week.
9. **Non-US ≠ no registration.** India's DLT and pre-registered sender-ID
   requirements in several APAC/ME destinations are A2P-equivalent. Confirm
   against the actual country before promising SMS works.

## Deferred

Shared-state (Redis) rate limiting and usage tiers → `plans/phase10.md`,
alongside the subscription decision behind them. A migration runner. Multi-region
or multi-replica deploy (needs the three per-process assumptions fixed first).
Prometheus/OpenTelemetry metrics — Step 5's structured logs plus LangSmith cover
Phase 7's needs. `TENANT_SOURCE=supabase` stays deferred (phase10 item 8).
Everything already in `plans/phase10.md` stays there.

## Est. effort

3–4 days, slightly over plan §15's 2–3 because the phase absorbs four confirmed
hardening gaps the plan text did not anticipate. Steps 0–2 are half a day and
mechanical. Step 9 is where the time goes, and Step 11's ordering is where the
day gets lost if it's taken casually.
