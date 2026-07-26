# Phase 4 — Multi-tenancy, Supabase, and per-tenant secrets

## Context

Phases 1–3 are done. The brain answers typed chat and live voice, `hotel-mzv` books
against a real Cal.com calendar, and two tenants already run side by side with different
trades and hours. But **nothing survives a restart**: jobs, calls, messages and
escalations live in `InMemoryStore` (`app/db/memory_store.py`), a process-local dict.
A redeploy silently erases every booking the business took.

Three consequences follow, and Phase 4 exists to fix all three:

1. **No durable system-of-record.** Plan §10 says Supabase holds the authoritative `jobs`
   row and the calendar is a link. Today the "authoritative" row is a dict that dies with
   the process, while Cal.com keeps the only real trace of the booking.
2. **One shared credential for every tenant.** `CALCOM_API_KEY` is `hotel-mzv`'s personal
   key. Any tenant flipped to `"calcom"` books into *hotel-mzv's calendar*, isolated only
   by `event_type_id`. CLAUDE.md names this as the Phase 4 blocker.
3. **Tenant isolation is application-layer only.** Convention #3 promises Supabase RLS as
   defence-in-depth. There is no database to enforce it in yet.

Plan §15's acceptance criterion — *"two tenants with different voices/hours/trades run on
one deployment, fully isolated"* — is already half met. What remains is durability,
per-tenant secrets, per-tenant **voice**, and the onboarding script that makes adding a
client something other than a git commit.

## Decisions locked

| | |
|---|---|
| **Driver** | **PostgREST over raw httpx**, reusing `shared_async_client` (`app/tools/http_client.py`). Zero new dependencies, matching the Phase 3 "raw httpx, no SDKs" precedent, and reusing the `mock_http` harness in `tests/conftest.py` wholesale. Decisive factor: `supabase-py`, `asyncpg` and `psycopg` all add compiled or heavy dependency surface, and CLAUDE.md documents a Windows Application Control policy that blocked a compiled DLL in `uuid_utils` 0.16+ and broke *every* import in the project. |
| **Tenant config** | **Hybrid, your choice.** `content/tenants/*.json` stays the file you edit; `scripts/sync_tenants.py` pushes it into Supabase. **The runtime read path stays on JSON in this phase** — see the note below; this is the one place I'm recommending less than what the option described. |
| **Store API** | Async **twins** (`aadd`, `aget`, …) alongside the existing sync methods, not a rename. This is the `invoke`/`ainvoke` convention the codebase already uses, and it costs **zero test edits** — see Step 3. |
| **RLS** | Real, not decorative. The backend mints a short-lived HS256 JWT per tenant carrying a `tenant_id` claim and a dedicated `app_backend` role; RLS policies read `auth.jwt() ->> 'tenant_id'`. The secret key is reserved for admin paths only. ⚠️ Gated on live verification — see Risk 1. |
| **Secrets** | **Supabase Vault**, read through a `SECURITY DEFINER` RPC in `public`. No client-side crypto, so no `cryptography` dependency. `SECRETS_ENCRYPTION_KEY` gets deleted from `.env.example`. |
| **Voice clone** | **In scope.** Record your own voice directly — do *not* clone on another platform and import the audio (quality degrades, and most TTS platforms' terms forbid using their output to build a voice elsewhere). Consent row required, enforced in code *and* by a DB constraint. |
| **Checkpointer** | **In scope — the second door.** Same Supabase Postgres, reached via `psycopg` instead of HTTP, because `langgraph-checkpoint-postgres` has no HTTP equivalent. Shipped as an **optional extra** with a fallback to `InMemorySaver`, so a blocked compiled DLL degrades durable memory only — never the app or the test suite. See Step 7. |

**Two doors into one database.** Supabase *is* Postgres plus services in front of it. This phase
uses both entrances to the same instance, each where it's actually better:

| Door | Used for | Why |
|---|---|---|
| **PostgREST over httpx** | jobs, calls, messages, escalations, tenants, Vault | Stateless, zero new dependencies, RLS enforced per request via a tenant-scoped JWT, and mockable with the `mock_http` harness that already exists |
| **psycopg (pooler)** | LangGraph checkpointer only | Needs real transactions and a held-open connection; there is no HTTP equivalent |

Keeping the split — rather than moving everything onto psycopg — is what makes the compiled
dependency **safe**: it becomes optional, and if it can't install, only conversation persistence
degrades. Everything else keeps working.

**On the tenant read path.** You picked hybrid, and the sync script, the `tenants`/`services`
tables and a `SupabaseTenantRepository` are all in this plan. But I'm keeping
`JsonFileTenantRepository` as the *default* read path for Phase 4, behind one setting, because
`tests/conftest.py:192` walks `get_repository().list_ids()` inside an autouse fixture that runs
**before** the `no_network` guard is armed — pointing that at Supabase makes 239 tests each open
a real socket with the guard unable to report why. Flipping the setting is one line once Step 4
is verified live. You keep the file-editing UX and the hot reload either way.

## What I need from you

**Supabase (blocks Steps 4 onward; Steps 1–3 proceed now)**

1. Create a project at supabase.com. **Pick the region closest to where the app will
   deploy** — every booking adds a round trip to the §13 budget, and §13 already calls for
   region co-location. If you're unsure of the hosting region, tell me and I'll recommend a
   pair.
2. From Settings → API: `SUPABASE_URL`, the anon/publishable key, and the secret
   (service_role) key.
3. **The load-bearing one:** Settings → API → JWT Settings — tell me whether a **legacy
   JWT secret (HS256)** is present, or whether the project shows only asymmetric signing
   keys. New projects increasingly default to asymmetric. The entire RLS design in Step 5
   hinges on this, and the fallback is a materially larger piece of work, so I want to know
   on day one rather than day four. If only asymmetric keys exist, check whether you can add
   a shared-secret **standby key**.
4. From Settings → Database → Connection string, the **Supavisor session-mode pooler** URI —
   the one whose host looks like `aws-0-<region>.pooler.supabase.com` on **port 5432**, *not*
   `db.<ref>.supabase.co` and *not* port 6543. Both of those wrong choices are live-only
   failures explained in Step 7. → `DATABASE_URL`.

**Voice clone (blocks Step 8)**

5. A clean 15–30 second recording of your voice — quiet room, phone voice memo is fine,
   no music or background talking. Read anything conversational.
6. Written consent, even though it's your own voice: one line, dated and signed
   ("I, <name>, consent to a synthetic voice being created from my recording for use in
   the AI Receptionist product"). This gets stored as a `voice_consents` row. CLAUDE.md
   convention #6 has no exceptions, and "it's my own voice" is exactly the case that gets
   waved through and then can't be evidenced later.

**Optional but valuable (Step 6's real acceptance test)**

7. A **second Cal.com account** with its own API key. Per-tenant secrets can only be
   *proven* by having two tenants book into two different calendars. Without it I can test
   the resolution logic but not the isolation it exists to provide.

---

## Free-tier operating constraints

You won't be paying for any platform. Three consequences are cheap to design for now and
expensive to retrofit, so they're built into the steps below rather than bolted on later.

**1. Checkpoint bloat will eat a 500MB database.** LangGraph never *updates* a checkpoint row —
it INSERTs a full serialized snapshot per superstep. Our graph runs ~5 node executions per turn
(`resolve_tenant → emergency_check → reason ⇄ tools`), so a ten-turn booking call writes ~50
rows, each carrying the message history. Reported real-world growth is ~56MB / 18k rows after a
week of *staging* traffic. OSS LangGraph has no TTL — that's a LangGraph Platform feature.

*Mitigation:* our conversations live for **minutes**, so a 24–48h TTL loses nothing real. A
`pg_cron` job (`0004_retention.sql`) deletes checkpoints older than 48h. `pg_cron` runs **inside**
Postgres — no external scheduler, no second service, consistent with "one roof".
`llm_history_messages: 20` (`app/config.py:49`) already bounds each snapshot, which helps.

**2. Free projects pause after 7 consecutive days with no database requests.** A paused project
means the receptionist answers the phone and cannot book. *Mitigation:* a scheduled GitHub
Actions workflow hitting `GET /health` (which does a trivial DB read) every ~3 days. Free, uses
the repo you already have, and doubles as an app keep-alive for free hosts that sleep idle
services.

**3. `calls.transcript` is the table that actually grows.** Recordings are URLs, so they cost
nothing — but full transcripts accumulate forever. Give them a retention window in the same
`pg_cron` job. Plan §16 already lists a PII retention policy as a legal to-do, so one job
satisfies both.

**One thing this phase cannot fix:** a free *hosting* tier that sleeps idle web services (Render
free sleeps after ~15 min with a ~50s cold start) is fatal for phone calls — Vapi times out while
the container wakes, and the caller hears nothing. That's a Phase 7 decision, but choose an
always-on free option rather than a sleeping one; the keep-alive above only partly compensates.

---

## Implementation

Each step ends with a green `pytest`. Steps 1–3 need no credentials.

### Step 0 — baseline
`pytest` → record the count (239 at time of writing).

### Step 1 — config and hygiene only (no behaviour change)

`app/config.py` — add as **real `Settings` fields**: `supabase_url`, `supabase_anon_key`,
`supabase_secret_key`, `supabase_jwt_secret`, `supabase_timeout_seconds=8.0`,
`secret_cache_ttl_seconds=300`, `tenant_source: Literal["json","supabase"]="json"`,
`database_url` (the psycopg pooler URI), `checkpoint_retention_hours=48`.

They must be real fields, not ad-hoc `os.environ` reads: `hermetic_settings`
(`tests/conftest.py:161-165`) strips only env vars *matching a field name*, so anything read
outside `Settings` leaks the developer's box into the suite — the lesson already recorded in
`plans/phase3.md`. Note `app/db/supabase.py:21` already reads `settings.supabase_url` behind a
`# type: ignore[attr-defined]`, i.e. the field genuinely does not exist yet.

Also in this step:
* **Delete `SECRETS_ENCRYPTION_KEY`** from `.env.example` — Vault makes it dead, and a
  second way to encrypt secrets is the same two-sources-of-truth trap that got
  `booking_provider` deleted in Phase 3.
* Add `"store": "memory" | "supabase"` to `GET /health` (`app/main.py:44`), so a
  mis-configured production deploy is one `curl` away from visible.
* **Move the `no_network` fixture above `isolated_runtime`** in `tests/conftest.py`. Same
  scope, same file → declaration order decides execution order, and today the socket guard
  is armed *after* the fixture that touches the repository.

### Step 2 — schema, RLS and Vault as plain SQL (zero Python)

`app/db/migrations/0001_schema.sql`, `0002_rls.sql`, `0003_vault.sql` — numbered plain SQL,
applied via the dashboard SQL editor or the Supabase CLI. No migration library, no new dep.

Tables per plan §6b: `tenants`, `services`, `jobs`, `calls`, `messages`, `escalations`,
`mcp_servers`, `voice_consents`. Shape rules that matter:

* **`text` primary keys.** Our ids are `job_<hex10>` (`app/db/models.py:17`), not UUIDs.
  Keeping `text` avoids an id-scheme migration touching every model and provider.
* **`timestamptz`** everywhere; the models are timezone-aware already.
* **`not null default ''`** on `Job.address` / `customer_*` and `OutboundMessage.to`/`body` —
  they are non-optional `str` in Pydantic, so a `NULL` coming back raises `ValidationError`
  mid-call.
* **`JobStatus` as `text` + `CHECK`**, not a PG enum — adding a status shouldn't need a
  locking migration.
* **`unique (tenant_id, provider_call_id)`** on `calls`, backstopping the upsert semantics.
* Every table: `enable row level security` **and `force row level security`**. `ENABLE`
  alone does not apply to the table owner, and dashboard-created tables are owned by
  `postgres` — so without `FORCE`, the policies you're testing are bypassed by your own
  admin path.
* Every table gets an explicit `grant` to `app_backend`. RLS sits *on top of* grants, not
  instead of them: a perfect policy with no grant is a 403 that reads like an auth bug, and
  `ENABLE` with no policy is deny-all that surfaces as **empty reads** — the same silent-empty
  failure mode as `plans/phase3.md` Risk 6.

`0003_vault.sql` adds a `SECURITY DEFINER` function in `public` (granted to the secret role
only) wrapping `vault.decrypted_secrets`. The `vault` schema is not PostgREST-exposed, and
must stay that way.

**One offline test earns its keep here:** regex `app/db/migrations/*.sql` and assert that for
every `create table public.X` there is a matching `enable row level security`, `force row level
security`, a `create policy` naming `tenant_id`, and a `grant`. ~25 lines, no dependency, and
it permanently closes "someone added a table and forgot RLS" — the real long-run failure, more
than the initial policies.

### Step 3 — async store twins + one latency bug (no Supabase yet)

`app/db/store.py` protocols gain **async** methods: `aadd`, `aget`, `aupdate`, `alist_jobs`,
`ascheduled_between`, `arecord_message`, `alist_messages`, `arecord_call`, `alist_calls`,
`arecord_escalation`, `alist_escalations`. `InMemoryStore` keeps every existing sync method
**byte-identical** and gains async twins that delegate.

Why twins rather than renaming: 11 of the 31 `get_store()` sites in tests live in
**synchronous** contexts — `isolated_runtime`'s `get_store().reset()` (`tests/conftest.py:179,
200`), and the sync `TestClient` tests in `test_api.py`, `test_vapi_webhooks.py`,
`test_vapi_llm.py`. `TestClient` runs the app on a different event loop in another thread;
converting those to async manufactures exactly the "Event loop is closed" flake that
`reset_shared_clients` (`app/tools/http_client.py:51-58`) was written to prevent. Twins cost
**zero test edits**, so when the suite goes red later, the signal is real.

Convert the 21 application call sites to the async API (`app/tools/booking/stub.py`,
`booking/calcom.py`, `messaging/twilio.py`, `messaging/stub.py`, `messaging/transfer.py`,
`app/channels/webhooks.py:55`, `app/tools/messaging_tools.py:32`, `scripts/chat_cli.py`), plus
four helpers that must become `async def`: `StubBookingProvider._require_job`,
`CalcomBookingProvider._require_job`, `TwilioNotifier._record_failure`, `chat_cli._print_jobs`.

Two fixes ride along:

* **`scheduled_between` must move onto the `JobStore` protocol.** It exists only on
  `InMemoryStore` (`memory_store.py:61`) today, yet `StubBookingProvider` depends on it — so
  the stub breaks the moment `get_store()` returns anything else.
* **Hoist it out of the loop.** `app/tools/booking/stub.py:60-66` calls `scheduled_between`
  *inside* the per-slot `while`, once per candidate slot across `horizon_days + 1` days. Free
  in memory; hundreds of round-trips against PostgREST, on the critical path. Query once
  before the loop, then filter in Python with the existing `Job.overlaps`
  (`app/db/models.py:47`). `tests/test_booking_provider.py:76,95` already cover the behaviour,
  so the refactor is verified for free. This matters beyond the stub: `northside-plumbing`
  runs on it, and it is the pinned provider for every test tenant.

Also widen the `store: InMemoryStore | None` type hints in the six provider constructors to
the protocol type — today they are a lie that `SupabaseStore` would violate.

### Step 4 — `SupabaseStore` over PostgREST

New `app/db/supabase_store.py` (replacing the placeholder `app/db/supabase.py`), and
`app/db/factory.py::get_store()` selecting on `settings.supabase_url` with a
`reset_store_cache()` hook. The factory must **not** live in `memory_store.py`: `get_store()`
is `@lru_cache`-d there (`memory_store.py:123`), so a settings branch would pin the decision
process-wide on first call and `reset_settings_cache()` would not clear it. Point
`tests/conftest.py:31` at the concrete memory singleton so the fixture is immune to the
factory's choice.

**Missing Supabase config is a hard boot failure when `app_env == "production"`.** Silently
falling back to in-memory in production means bookings vanish on redeploy with no error.

PostgREST semantics that silently change behaviour if missed:

* `Prefer: return=representation` on every write, and return the **server's** row — otherwise
  a write that never landed is masked by the locally-constructed object.
* `update()` must raise `KeyError` on a missing row, mirroring `memory_store.py:56`. A `PATCH`
  matching zero rows returns **200 with `[]`**, not an error — without an explicit row-count
  check, cancelling a foreign tenant's job silently "succeeds".
* Never f-string a query: ISO-8601 offsets contain `+`, which is a literal space in a URL.
  Pass `params={...}` and let httpx encode.
* `record_call`'s **upsert-by-`provider_call_id`** (`memory_store.py:81-95`) keeps the existing
  `id` and replaces the rest. PostgREST's `merge-duplicates` overwrites every column sent,
  including our app-generated `id`. Do SELECT-then-insert-or-patch, exactly as the memory store
  does — it's a webhook, not the latency budget.
* Empty-body guard, mirroring the existing one in `calcom.py:334`.

New `tests/test_supabase_store.py` on `mock_http`: exact URL/params/`Prefer` per method, the
empty-body path, zero-row PATCH → `KeyError`, the SELECT-then-write `record_call` path, and a
parametrised assertion that **no request ever lacks `tenant_id=eq.`** — the offline half of
tenant isolation.

### Step 5 — real RLS via a tenant-scoped JWT

`app/db/auth.py` — ~20 lines of stdlib `hmac`/`hashlib`/`base64` minting an HS256 JWT with
`iss`, `aud`, `exp`, `sub`, `tenant_id` and `"role": "app_backend"`. Sign-only; we never
verify, so there is no verification logic to get wrong and no reason to add PyJWT.

Three specifics:

* **`role: authenticated` is wrong.** That is GoTrue's role for end users; the moment a
  customer-facing dashboard exists on Supabase Auth, those users inherit every grant written
  for the backend. Create `app_backend` (`nologin`, granted to `authenticator`) and mint that.
* **`apikey` is still mandatory** alongside `Authorization`. Omitting it is a 401 that looks
  exactly like an RLS denial.
* **Do not fold the JWT into the `shared_async_client` key.** That memoises by key and sets
  headers at construction — a rotating per-tenant token would mint a new client, a new
  connection pool and a cold TLS handshake per tenant per refresh, growing `_clients`
  unboundedly. That is precisely the cost `http_client.py` exists to eliminate. Build **one**
  client keyed on the project URL carrying `base_url` + `apikey`, and pass
  `headers={"Authorization": f"Bearer {jwt}"}` per request.

Cache minted tokens at 60s TTL against a 5-minute `exp` — not for CPU (HMAC is microseconds)
but to have one place to invalidate.

### Step 6 — per-tenant secrets (the point of the phase)

New `app/tenancy/secrets.py`, copying the shape of `app/tenancy/loader.py:21-23,42-61` exactly
(`RLock` + `dict[str, tuple[float, T]]` + TTL + `clear_*_cache()`). It cannot live in the
providers — `app/tools/providers.py:21-36` constructs a fresh provider on *every* tool call.

Resolution: **per-tenant vault secret → global env fallback.** The critical subtlety:

> **Distinguish "absent" from "errored".** Absent (RPC returns null) → env fallback is
> legitimate. Errored (timeout, 5xx, 401) → **never** fall back. Today `CALCOM_API_KEY` is
> `hotel-mzv`'s account; if tenant B's vault read times out and falls through to env, B's
> guest is booked into **hotel-mzv's real calendar**. That is a cross-tenant leak, it violates
> convention #3, and it is invisible in logs.

Serve a **stale cached secret past TTL when the vault is down** so a Supabase blip doesn't take
booking offline. Map the errored case to `BookingError` / `MessagingError`, never a bare
exception — `booking_tools.py` turns `BookingError` into a recoverable `ERROR:` string, while an
escaped exception produces `FALLBACK_LINE` with the booking silently lost.

Consequence: `CalcomBookingProvider._get_client()` (`calcom.py:66-81`) and
`TwilioNotifier._get_client()` (`twilio.py:45-59`) become `async def`. Key the shared client
`f"calcom:{tenant_id}:{sha256(key)[:12]}"` — **never put the raw secret in a module-global dict
key**, where every traceback prints it.

Harden `messaging_tools.py:32` while here: a transient 5xx surfacing as `None` currently hands
Llama *"no job … Check the job_id from book_job"*, inviting it to re-book a booking that already
exists in a real calendar. Store errors must raise, and `send_confirmation` must catch them
separately into the phase-3-shaped string already at `messaging_tools.py:61-65` — *the booking
IS confirmed, read the details out, do not book again.*

### Step 7 — durable conversation memory (the psycopg door)

`app/brain/graph.py:48-57` compiles with `InMemorySaver`, so a redeploy drops in-flight context.
Voice recovers (`thread_is_cold` reseeds from Vapi's transcript); web chat silently does not.

**Ship it as an optional extra.** `pyproject.toml` gains
`postgres = ["langgraph-checkpoint-postgres>=2.0", "psycopg[binary,pool]>=3.2"]`. `get_graph()`
uses `AsyncPostgresSaver` when `DATABASE_URL` is set *and* the import succeeds, and otherwise
falls back to `InMemorySaver` with a WARNING. That fallback is the whole reason this is safe: if
Windows Application Control blocks psycopg's DLL the way it blocked `uuid_utils`, only durable
memory degrades — `chat_cli`, the server and all 239 tests keep working. Linux deploy installs
normally. Add the active saver to `GET /health` alongside `store`.

Four connection details, each a **live-only** failure if missed:

* **Use the Supavisor pooler host, never `db.<ref>.supabase.co`.** The direct host has resolved
  to **IPv6 only** since Jan 2024 and most free hosting platforms have no IPv6 egress. The
  symptom is a connection timeout that reads exactly like a firewall misconfiguration.
* **Session mode (pooler port 5432), not transaction mode (6543).** Transaction mode doesn't
  support prepared statements, and `AsyncPostgresSaver` dies on it with
  `DuplicatePreparedStatementError` / `InvalidSqlStatementName` — a well-documented, widely-hit
  LangGraph-on-Supabase failure.
* **Pass `prepare_threshold=0`** regardless, as belt-and-braces if the pooler mode ever changes.
* **Pin the pool small** (`min_size=1, max_size=3`). Free tier is 500MB shared RAM; a default
  pool of 4+ per instance burns connections for no benefit at our concurrency.

**Keep the checkpoint tables out of `public`.** `.setup()` creates `checkpoints`,
`checkpoint_blobs`, `checkpoint_writes` and `checkpoint_migrations` in the connection's default
schema — and **PostgREST exposes everything in `public`**. Left there, every conversation
transcript becomes readable over the REST API with the anon key, LangGraph owns that schema so
our migration lint (Step 2) won't catch it, and nothing errors. Create a `langgraph` schema and
set `search_path` on the connection (`?options=-csearch_path%3Dlanggraph`); PostgREST only
exposes schemas it's configured for, which closes this completely. ⚠️ VERIFY the exact
search_path form against the installed version before relying on it.

**Isolation here is by thread-id prefix, not RLS.** Threads are already tenant-prefixed
(`f"{tenant}:vapi:{call.id}"`) and the checkpointer connects as a privileged role, so the JWT/RLS
path does *not* cover these tables. State that plainly in CLAUDE.md rather than letting the RLS
work imply coverage it doesn't have.

Write `app/db/migrations/0004_retention.sql` here too — the `pg_cron` job from the free-tier
section, covering both checkpoints (48h) and `calls.transcript`. It can only be written once
`.setup()` has created the tables it prunes.

Tests stay offline: the fallback path is unit-testable (no `DATABASE_URL` → `InMemorySaver`, no
warning swallowed), and the pooler behaviour is live-verification items 13–15.

### Step 8 — Cartesia voice clone + consent

`voice_consents` table: `tenant_id`, voice owner name, signed-artifact URL/hash, consent
timestamp, sample audio hash, granting user. `tenants.voice_id` settable **only** when a
consent row exists — enforced in `onboard_tenant.py` *and* as a DB constraint, because
"no exceptions" has to survive someone running SQL by hand.

The clone upload is multipart audio: give it its **own** `shared_async_client` key with a long
timeout. Reusing the 15s booking-shaped timeout is a silent onboarding failure.

A clone changes `VoiceSettings.voice_id`, which is baked into the Vapi assistant at
provisioning — so onboarding must re-run `provision_vapi`, the same lesson as the escalation
number in CLAUDE.md.

### Step 9 — `scripts/onboard_tenant.py` and `scripts/sync_tenants.py`

`onboard_tenant.py` finally implements plan §6d, writing **both** the Supabase row and the
`content/tenants/<id>.json` file: tenant + services + hours → Twilio number → consent check →
Cartesia clone → Vapi assistant → booking credentials into the vault → MCP registry →
status `active`.

`sync_tenants.py` pushes edited JSON into `tenants`/`services` — the hybrid you chose. Both are
idempotent and re-runnable, like `provision_vapi`.

**Decide what `"stub"` means, in this step.** `BookingSettings.provider` already accepts
`"supabase"` (`app/tenancy/models.py:90`) and `get_booking_provider` raises a bare
`NotImplementedError` for it (`providers.py:36`) — which is *not* a `BookingError`, so it
escapes the tool's typed handler. Meanwhile `StubBookingProvider` + a Supabase store stops
being a stub: it becomes a legitimate no-external-calendar provider with durable jobs and
DB-backed conflict detection, which is what `northside-plumbing` actually wants. Either wire
`"supabase"` to it and rename the class, or delete `"supabase"` from the `Literal`. Leaving
both is the two-sources-of-truth trap.

### Step 10 — docs
CLAUDE.md "Current state" + new gotchas (the two doors, the pooler/session-mode rule, the
`langgraph` schema, thread-prefix isolation); `content/README.md` (the sync step, per-tenant
credentials); `README.md` (the stubbed/real table); a plan §6 amendment recording the hybrid
tenant-config decision, the PostgREST-over-SDK choice, and the free-tier retention jobs.

---

## Testing

Everything above stays offline on `mock_http` + the autouse `no_network` guard. The one new
offline test worth naming beyond the per-step suites is the migration lint (Step 2) and the
"every request carries `tenant_id=eq.`" parametrised assertion (Step 4).

**Live verification — genuinely untestable offline, in order:**

1. **Does the project expose a legacy HS256 JWT secret, and does PostgREST accept a token
   signed with it?** Gate for Step 5 — do this *first*, before writing the RLS design.
2. `apikey` + `Authorization` accepted together; confirm which key form is required.
3. `app_backend` granted to `authenticator`, and PostgREST actually switches to it.
4. **RLS denies for real:** a tenant-A token reading `/rest/v1/jobs` returns only A's rows, and
   a POST carrying `tenant_id=B` is rejected by `WITH CHECK` rather than silently written.
5. `FORCE ROW LEVEL SECURITY` verified by querying as the table owner — the check that catches
   decorative RLS.
6. `vault.decrypted_secrets` unreachable via PostgREST (expect 404); the wrapper RPC's grants
   correct; a secret round-trips.
7. Resent Vapi end-of-call report → one `calls` row, id preserved.
8. DST/timezone round-trip: book a Karachi tenant and a New-York tenant across a DST boundary;
   confirm the spoken time matches the calendar (the CLAUDE.md Karachi/New-York lesson).
9. **Latency with Supabase in the loop:** p50 for `check_availability`, `book_job`,
   `send_confirmation` against the §13 600–800ms budget, plus the app-region ↔ DB-region gap.
   First phase where co-location is measurable.
10. **Two tenants, two Cal.com accounts, per-tenant vault keys → each books into the correct
    account.** The acceptance criterion for the whole secrets workstream.
11. Cartesia clone → `voice_id` → re-run `provision_vapi` → the assistant speaks in your voice.
12. Production boot with `SUPABASE_URL` unset **fails hard**; half-configured also fails hard.
13. `AsyncPostgresSaver` connects through the **pooler** host in session mode, `.setup()`
    succeeds, and a conversation survives a process restart (talk in `chat_cli`, restart the
    server, continue the same thread).
14. **Checkpoint tables are not reachable at `/rest/v1/checkpoints`** (expect 404) — the proof
    that the `langgraph` schema actually hid them.
15. `pg_cron` retention job is scheduled, and a checkpoint older than the window is gone after
    its next run.
16. Free-tier keep-alive: the GitHub Actions workflow runs on schedule and the project has not
    paused after a quiet week.

## Risks

1. **No legacy HS256 secret on a new project.** Supabase now defaults toward asymmetric signing
   keys; the ES256/RS256 fallback needs `cryptography`, a compiled dependency CLAUDE.md rules
   out on this box. Mitigation: live-verify **first** (item 1 above). If absent, try adding a
   shared-secret standby key; failing that, fall back to secret-key + application scoping with
   RLS policies still installed and `FORCE`d, and document honestly that RLS is dormant until a
   non-secret connection path exists. Knowing this on day one is worth more than any mitigation.
2. **Vault-unreachable → wrong tenant's calendar.** Covered by absent-vs-errored above; calling
   it out separately because it is the single most damaging bug this phase can ship.
3. **Decorative RLS.** `ENABLE` without `FORCE`, or policies without grants. Covered by the
   migration-lint test plus live items 4–5.
4. **Empty reads look identical to "no bookings yet".** A deny-all policy, a missing grant and a
   genuinely empty table are indistinguishable to the caller — the bot politely offers a
   callback forever with nothing in the logs. Same failure mode as Phase 3's Risk 6. Mitigate by
   logging a WARNING on a zero-row read where rows were expected.
5. **`get_store()`'s `lru_cache` pinning the backend process-wide.** Handled by moving the
   factory out of `memory_store.py` and exposing `reset_store_cache()`.
6. **A developer with `SUPABASE_URL` exported flipping the whole suite to Supabase.** Handled by
   Step 1's real `Settings` fields; `hermetic_settings` then strips it like any other.
7. **Latency creep.** Every booking now costs extra round trips (store write, possibly a vault
   read). Mitigated by the client cache, the secret cache, the `scheduled_between` hoist, and
   region co-location — but it must be *measured* (live item 9), not assumed.
8. **Wrong connection string → dead on deploy, fine locally.** IPv6-only direct host, or
   transaction mode breaking prepared statements. Listed separately from Step 7 because both
   symptoms (a timeout, a duplicate-prepared-statement crash) look like something else entirely
   and cost a day each to trace back to the URI.
9. **Checkpoint tables exposed through PostgREST.** Silent by construction — transcripts
   readable with the anon key and nothing errors. Closed by the `langgraph` schema; live item 14
   is the only thing that actually proves it.
10. **psycopg blocked on this Windows box**, as `uuid_utils` was. Contained by the optional
    extra plus the `InMemorySaver` fallback: development and the entire suite continue unaffected,
    and the feature still works on the Linux deploy.
11. **Free tier fills up quietly.** 500MB, no backups. Retention jobs are the mitigation, but
    add a size check to the live checklist and revisit once real call volume exists.

## Deferred

Supabase as the tenant *read* path (one setting, once Step 4 is verified live); an admin
dashboard (Phase 8); booking idempotency keys; `cancel`/`reschedule` native tools; Google
Calendar behind the existing `BookingProvider` interface; LangGraph Platform's built-in
checkpoint TTL (paid — `pg_cron` covers it for free).

## Est. effort

5–7 days. Steps 1–3 are mechanical and need no credentials. Step 5 is the schedule risk, entirely
because of Risk 1 — which is why live item 1 comes before any of it. Step 7 is small in code and
almost entirely about getting the connection string right the first time.
