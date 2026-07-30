# Phase 8 — Analytics dashboard + per-tenant admin

## Context

Phases 1–7 are done and deployed. The brain answers voice and chat for two
tenants, books a real Cal.com calendar, persists behind real RLS, loads
per-tenant MCP tools, and runs on Railway in `us-east4` against Supabase in
`us-east-1`. What it has never had is a **surface**. Every operational question
— "how many calls did hotel-mzv take this week", "why did that booking fail",
"change the greeting" — is answered today by a shell, a git commit and a
redeploy.

Plan §15's Phase 8 line is *"Tavus/Simli video avatar add-on; analytics
dashboard; per-tenant admin."* Two of those three ship here. **The video avatar
moves to `plans/phase10.md`** by your decision — which is well-timed, because
§12's premise for it is now factually wrong: **Vapi discontinued its Tavus
integration** (Vapi staff, 20 Jun 2025). "Tavus (already integrated with Vapi)"
describes a path that no longer exists; the replacement is a browser-side WebRTC
composition, a genuinely different piece of work. Better planned honestly later
than built on a stale premise now.

Five things are confirmed in the code, not anticipated, and they shape
everything below:

1. **`TenantRepository` (`app/tenancy/repository.py:21-30`) is read-only** — five
   read methods, no `save`. Every `TenantConfig` model is `frozen=True`. There is
   no write seam for config at all.
2. **The tenant read path is `content/tenants/*.json`, baked into the image** by
   `infra/Dockerfile`. A config edit written to the container's filesystem
   survives until the next restart, then vanishes silently.
   `settings.tenant_source` declares a `"supabase"` branch (`app/config.py:150`)
   and nothing implements it.
3. **There is zero aggregation anywhere** — no count/sum/group-by/limit/offset on
   any store protocol or implementation, and no view or aggregate function in any
   of the seven migrations. `alist_calls(tenant_id)` has no time filter, no limit,
   and selects full transcripts. `ChatLog` cannot list a tenant's sessions at all.
4. **There is no `StaticFiles` mount.** `app/main.py` serves exactly two static
   files, one at a time, via `FileResponse`. CORS is
   `allow_methods=["GET","POST","OPTIONS"]` — no `PUT`/`PATCH`/`DELETE`.
5. **`app/brain/metrics.py` is process-global and explicitly not per-tenant.** Its
   own docstring records that the `ContextVar` fix already failed. Per-tenant LLM
   cost is not available and must not be implied.

**Outcome:** a same-origin admin surface behind a dedicated bearer token, where an
operator can see what each tenant's receptionist actually did and change what it
says — taking effect on the next turn, no redeploy — with the route shape,
principal abstraction and read path already in the form real per-tenant login
will need.

## Decisions locked

| | |
|---|---|
| **Scope** | Analytics + per-tenant admin. Video avatar → `plans/phase10.md` item 13. |
| **Tenant read path** | **`TENANT_SOURCE=supabase` is pulled forward and is Step 1** — not optional, see below. `content/tenants/*.json` becomes seed + boot fallback, never runtime truth in production. |
| **Write seam** | **The repository protocol stays read-only.** Writes go through a new `app/tenancy/admin.py` wrapping the existing `sync_tenant()` on the Supabase secret key — the operator/tenant split `app/tenancy/sync.py:8-11` already argues for. |
| **Validation** | **Pydantic, entire.** Whole-document PUT → `TenantConfig.model_validate` → `ValidationError.errors()` → 422 with field paths. No hand-written rules. |
| **Analytics** | **SQL views with `security_invoker = true` + one JWT-derived RPC**, read through `SupabaseStore`'s existing tenant-scoped JWT. Not Python-side aggregation, not PostgREST aggregate syntax. |
| **Cross-tenant rollup** | **Per-tenant loop over the same tenant-JWT views**, never the secret key. |
| **Admin auth** | **New `ADMIN_AUTH_TOKEN`, `ADMIN_ENABLED=false` default, fails *closed*** — the one deliberate break with this codebase's fail-open-when-unconfigured convention. |
| **UI** | **Second Vite app under `admin/`**, mirroring `widget/`, with its own buildhash guard. Served **same-origin**. |
| **Process model** | Still one worker, one replica. Phase 8 adds a *fourth* reason (cache invalidation is per-process), not a new constraint. |

## What I need from you

**Nothing blocks Steps 0–9.** Two things, neither urgent:

1. **A go/no-go on the read-path flip** (it's implicit in choosing a config-editing
   admin panel — see the next section for what the panel collapses to if you
   decline). No new account or credential either way.
2. **`ADMIN_AUTH_TOKEN`** — I'll generate a 32+ char token; you set it as a Railway
   variable at Step 10. It has the largest blast radius of any secret in the repo
   (see Risk 6), so it should not be shared with anything else.

Live verification (Step 10) uses the existing Supabase project and Railway
service. Node 20+ is already present from Phase 5.

---

## Why the read-path flip is the whole phase

`app/tenancy/loader.py:26-31` hard-wires `JsonFileTenantRepository`. The JSON is
baked into the image. Railway's filesystem is ephemeral across restart and
redeploy. So an admin panel that writes JSON to disk produces edits that
disappear on a schedule nobody controls.

It is worse twice over:

- `JsonFileTenantRepository._parsed` (`repository.py:38`) is a permanent
  in-process dict with **no TTL and no link to `clear_tenant_cache()`** — that
  function clears the *loader's* TTL cache (`loader.py:42`), not the repository's
  parse cache. A file write is invisible to the running process until restart,
  even on a dev box. (`invalidate()` exists at `repository.py:84` and nothing
  calls it.)
- If instead the panel writes only through `sync_tenant()` — the durable,
  already-existing write path — it writes to tables **nothing reads**.
  `app/tenancy/sync.py:6` says so verbatim: *"Nothing reads them today."*

That second case is the failure mode to design against. Call it **the phantom
edit**: the operator changes hotel-mzv's greeting, the panel returns 200,
`public.tenants.config` is genuinely correct in Postgres, and the receptionist
keeps speaking the old greeting forever. Nothing errors, nothing logs, both
halves work perfectly in isolation. This is the worst thing Phase 8 could ship,
and the only thing preventing it is the read path pointing where the write path
writes.

**If you decline the flip**, the honest scope collapses to: read-only analytics, a
read-only config viewer, and an "export the edited JSON so you can commit it"
flow ending in `git push` + redeploy. Defensible — just not "per-tenant admin".

### What the `supabase` branch actually requires

**The protocol is synchronous, and that is the central constraint.**
`TenantRepository.get()` is `def`, not `async def`, and its callers are graph
nodes, every native tool, prompt assembly, `resolve_tenant_id`, and conftest's
autouse `isolated_runtime`. Making it async is a rewrite of the whole call graph.
Blocking `httpx` inside the sync `get()` is worse: a cold-cache read is a
50–200ms blocking round trip **on the event loop**, stalling every concurrent
request including other callers' in-flight SSE streams — rare, intermittent, and
impossible to attribute.

**So: snapshot at boot, refresh out of band.** New
`app/tenancy/supabase_repository.py` loads every tenant into an immutable dict
once, in `lifespan` where `await` is available, and serves every sync
`get`/`list_ids`/`find_by_*` from that dict with zero request-path I/O.

This is not a compromise — it is *exactly what `JsonFileTenantRepository` already
does*. That class caches every parsed tenant forever, and all three `find_by_*`
methods are already O(n) scans over the full set. Identical semantics, different
source. Two wins fall out: `find_by_assistant_id` needs
`config->'vapi'->>'assistant_id'`, an unindexed JSONB path that stays free as a
dict scan; and the whole snapshot is smaller than one call transcript.

New module rather than extending `repository.py`, mirroring how
`app/db/supabase_store.py` sits apart from `memory_store.py` — it keeps httpx out
of the module every test imports.

**The `config` JSONB round-trip has two verified traps:**

- **`services` is in `_TENANT_COLUMNS` but `public.tenants` has no `services`
  column.** `model_dump(exclude=...)` strips services from the blob and
  `_tenant_row` never puts them back — services live *only* in `public.services`.
  Hydration must join, via PostgREST's embedded resource
  `GET /tenants?select=*,services(*)` (the FK exists at `0001_schema.sql:42`).
  ⚠️ VERIFY the embed name resolves as `services` on the live project — a
  mis-detected relationship is a 400 with an unhelpful message.
- **`mcp_servers` exists in two places at once** — in the `config` blob (it is
  *not* in `_TENANT_COLUMNS`) and in `public.mcp_servers`, which `MCP_SOURCE`
  reads independently. Do not reconcile them; hydrate `TenantConfig.mcp_servers`
  from the blob and leave `app/mcp/registry.py` alone.

**Per-row failure must not poison the snapshot.** A `config` blob that fails
`model_validate` is caught, logged, skipped, and replaced by its JSON copy if one
exists — the per-row skip posture `app/mcp/registry.py` already uses. One bad
tenant taking down every tenant's boot is not acceptable.

**Boot behaviour**, in `lifespan` before `get_graph()`: build
`SupabaseTenantRepository(fallback=JsonFileTenantRepository(...))`, `await
repo.refresh()` (never raises), `loader.set_repository(repo)`. Wholesale failure
logs ERROR, sets `degraded`, serves baked-in JSON — strictly better than today
(where JSON is the *only* source) and matching the established posture everywhere
else (MCP → `[]`, checkpointer → `InMemorySaver`, transcript writes → a log line).

State the contrast explicitly, because someone will ask: this is the opposite of
`app/tenancy/secrets.py`'s "a vault error is never absent" rule, deliberately.
There, falling back books one tenant into another's real Cal.com account — wrong
*and* cross-tenant. Here it serves the tenant's own last-committed config —
wrong-but-safe. Different blast radius, different rule.

But the fallback re-creates the phantom edit in different clothes: an edit landed
in Supabase, the snapshot fell back to JSON, and the change looks reverted. So the
degraded state must be **loud** — a `problems[]` entry in `/health`, a WARNING per
served-from-fallback tenant, and a banner in the admin UI off `/admin/api/session`.

**`content/tenants/*.json` become seed, fallback, and what
`scripts/onboard_tenant.py` writes.** Dev and tests stay `TENANT_SOURCE=json` —
zero behaviour change, zero test churn. Two consequences need code, not just docs:

- **The sync stomp.** `scripts/sync_tenants.py` is a blind whole-`config` upsert;
  after Phase 8 it silently reverts every panel edit. It gains `--force` (and
  refuses without it when `TENANT_SOURCE=supabase`) plus `--export` to pull
  Supabase → JSON so committed files can be refreshed from live truth.
- **`sync_tenant()` only upserts, never deletes.** Removing a service or MCP
  server in the panel leaves an orphan row that `MCP_SOURCE=supabase` keeps
  loading and the `services(*)` embed keeps returning. Step 6 adds
  delete-of-absent-children.

### What remains of the old blocker — verified

The historic reason for deferring this (`plans/phase4.md`'s "On the tenant read
path") **is already fixed**: `tests/conftest.py` declares `no_network` (line 180)
*before* `isolated_runtime` (line 206), with a comment recording exactly why; and
`hermetic_settings` strips `TENANT_SOURCE` because `tenant_source` is a real
`Settings` field, so tests default to `"json"` regardless of `.env` or shell.

**One prerequisite remains.** `isolated_runtime` calls `clear_tenant_cache()` but
**never resets `loader._repository`** — only `override_tenant` saves/restores it,
opt-in per test. The moment any test installs a `SupabaseTenantRepository` and
forgets to restore, every later test inherits it and `no_network` turns that into
dozens of unrelated failures with a misleading message. **Add
`loader.set_repository(None)` to `isolated_runtime`'s setup and teardown** — one
line, and a prerequisite for Step 1's tests, not a nicety.

---

## The admin write path

**The protocol stays read-only.** Adding `save()` would force
`JsonFileTenantRepository` and conftest's `_OverrideRepository` to implement a
write neither has business implementing, and hang a mutation method off the object
every tool and graph node holds. Read and write here have different auth
(tenant-scoped JWT vs. secret key), different failure semantics and different
callers — the same split that already exists between `app/db/supabase_store.py`
and `app/tenancy/sync.py`.

**New `app/tenancy/admin.py`**, one function:

```python
async def save_tenant(config: TenantConfig, *, expected_version: str | None) -> TenantConfig
```

In order: pre-check voice consent → upsert via the existing `sync_tenant()` →
delete absent children → `clear_tenant_cache()` + refresh the repository
(duck-typed `getattr(repo, "refresh", None)` / `getattr(repo, "invalidate",
None)`, so json mode is a cheap no-op). Extending `sync_tenant()` rather than
writing a parallel writer is deliberate — its docstring exists specifically so the
scripts "can't drift into writing the row shape differently." The admin API is the
third caller.

**Frozen models are an asset.** The API never mutates: fetch current →
`model_dump(mode="json")` → merge the request body → `TenantConfig.model_validate`
→ save. Validation is therefore **Pydantic's existing validators, for free** —
`_known_weekdays`, `_real_timezone`, `_unique_service_slugs`,
`_calcom_tenants_declare_event_types`, `DayHours._close_after_open`,
`McpServerConfig._legal_tool_prefix`, `_transport_has_its_endpoint`, and every
`Field(gt=…, le=…)`. The route's whole job is mapping `ValidationError.errors()`
to a 422 whose `loc` tuples the UI attaches to inputs.

That is also the argument for **whole-document PUT** over field-level PATCH:
`_calcom_tenants_declare_event_types` is cross-field by construction (it compares
`booking.event_type_id` against every service override). A `PATCH /booking`
endpoint would re-derive that rule in a second place, and the two would drift.

**Optimistic concurrency.** Two tabs editing one tenant is last-write-wins,
silently. Add `updated_at timestamptz not null default now()` in `0009`, set
**explicitly in `_tenant_row`** (not by trigger — `0005` already shows triggers on
this table have teeth), return it as an ETag from GET, require it on PUT, 409 on
mismatch.

**Cache invalidation at >1 replica — the honest limitation.** Loader cache and
repository snapshot are both per-process. At one replica (already hard-required by
`TurnCounter`, `widget_auth.py`'s fallback secret, and the rate limiter) an admin
write invalidates the only cache that exists and the change is live next turn. At
two, replica B serves stale config for up to `tenant_cache_ttl_seconds` (300s) —
and the operator notices first, because they're *looking at* what they just
changed. Accept and document as the fourth reason; cross-replica invalidation
(LISTEN/NOTIFY or Realtime) goes to phase10 beside the Redis rate limiter, same
root cause.

### The voice-consent trigger — a latent bug this phase would expose

`0005_voice_consent.sql:14-16` comments that the trigger "only fires when voice_id
is being newly set OR changed, not on every ordinary update." **Under the upsert
path that is false.** The function computes
`old_voice_id := case when tg_op = 'INSERT' then null else old.config…end`, and
PostgREST's `Prefer: resolution=merge-duplicates` is `INSERT … ON CONFLICT DO
UPDATE` — whose **BEFORE INSERT** pass fires with `tg_op = 'INSERT'`. So on a
tenant that already has a `voice_id`, `new_voice_id is distinct from null` is true
even when the voice did not change, and the consent check fires.

**Consequence: editing the greeting on a tenant with a cloned voice would be
rejected for lacking voice consent.** Latent today only because no tenant has ever
been cloned. ⚠️ VERIFY live (checklist item 9); `0009` carries the fix — replace
the `old`-row comparison with an existence check against the stored value:

```sql
if not exists (select 1 from public.tenants t
               where t.tenant_id = new.tenant_id
                 and t.config -> 'voice' ->> 'voice_id' = new_voice_id)
```

Surface it as a usable error in `app/tenancy/admin.py`, both halves: a **pre-check**
reading `/voice_consents?tenant_id=eq.X&limit=1` before any write that changes
`voice.voice_id`, 409ing with copy naming the exact `onboard_tenant --voice-sample
… --consent-url …` invocation; and **trigger-error mapping**, catching
`TenantSyncError`, parsing PostgREST's body for `P0001` / the substring
`voice_consents`, re-raising as `VoiceConsentRequiredError` → 409. ⚠️ VERIFY
whether PostgREST maps an unqualified `raise exception` to 400 or 500 on this
project's version — the pre-check exists partly so the good message doesn't depend
on it.

---

## The analytics data layer

Python-side aggregation is rejected: `alist_calls` has no limit, no time filter,
and selects `transcript`, so "calls this week" pulls every transcript across the
wire on each page load — an unbounded latency problem *and* a PII problem — and
chat volume is uncomputable that way at all. PostgREST aggregate syntax is
rejected: gated by `db-aggregates-enabled`, shipped disabled-by-default on some
Supabase releases, failing as an opaque 400.

**`0008_analytics.sql` — views with `WITH (security_invoker = true)` plus one
JWT-derived RPC**, read through the existing tenant-scoped JWT. Four reasons, by
weight:

1. **It keeps the tenant JWT on every analytics read** — what makes "designed for
   tenant login later" real rather than aspirational.
2. Aggregation happens where the data is; no transcript crosses the wire.
3. One versioned migration, reviewed like every other schema change here.
4. `security_invoker` makes the view isolation model *identical* to the tables' —
   same policies, same `auth.jwt() ->> 'tenant_id'`, no second isolation story.

**`security_invoker` is not optional and is the phase's sharpest edge.** A plain
view in `public` is PostgREST-exposed automatically and runs with the *owner's*
privileges — it does **not** apply the underlying tables' RLS. A
`daily_call_stats` view without it hands every tenant's call volume to anything
holding the anon key, and **`tests/test_migrations.py` would not catch it** (that
lint only inspects `create table public.X`). So Phase 8 extends the lint: **every
`create view public.X` must carry `with (security_invoker = true)` and a grant to
`app_backend`.** Same reasoning as the original lint, new object type.

**Objects** — all `security_invoker`, all granted `select` to `app_backend` **and
nothing else**:

- `daily_call_stats` — day, calls, total/avg seconds, `cost_usd`, plus
  `count(*) filter (where ended_reason like 'error%')`. **No transcript column.**
- `daily_job_stats` — jobs/day by `status` and `channel`.
- `daily_chat_stats` — sessions and messages/day. The only reason chat volume
  becomes computable.
- `daily_escalation_stats` — escalations/day by `reason`/`channel`.
- `tenant_overview` — one row per tenant, 30-day totals + last-activity stamps.
- `tenant_metrics(from_day date, to_day date)` — `language sql stable`, **no
  `security definer`**, returning the windowed bundle in one round trip. It takes
  **no tenant_id parameter** — RLS scopes it, the identical rule
  `0003_vault.sql::get_tenant_secret` established.

**Indexes, same file.** `calls` has only `unique (tenant_id, provider_call_id)`;
`escalations` and `messages` have none. Every daily view groups by `(tenant_id,
created_at)`. Add that index on `calls`, `escalations`, `messages`,
`chat_sessions` — absent, this is a slow page in six months nobody attributes to
Phase 8.

**Cross-tenant rollups: per-tenant loop, never the secret key.** The landing page
fans out over `get_repository().list_ids()` with `asyncio.gather`, bounded
concurrency and a ~30s in-process cache. Using the secret key would create a
second RLS-bypassing read path to delete and re-audit the day tenant login lands —
precisely the rewrite we're avoiding. Named failure mode: N+1 round trips; when
tenant count makes that hurt, *that* is the moment for a `security definer`
operator RPC, and that note goes in phase10.

**New protocol methods** — an `AnalyticsStore` Protocol in `app/db/store.py` with
sync + async twins per house convention, plus one `ChatLog` addition:

```python
async def atenant_metrics(tenant_id, *, since: date, until: date) -> TenantMetrics
async def adaily_series(tenant_id, *, since: date, until: date) -> list[DailyMetrics]
async def alist_recent_calls(tenant_id, *, limit: int = 50, since=None) -> list[CallSummary]
async def aget_call(tenant_id, call_id) -> Call | None
async def alist_chat_sessions(tenant_id, *, limit: int = 50, since=None) -> list[ChatSession]  # ChatLog
```

`TenantMetrics` / `DailyMetrics` / `CallSummary` are new models in
`app/db/models.py`. **`CallSummary` deliberately omits `transcript` and
`recording_url`** — the list surface is PII-free by construction, and `aget_call`
is the only route to a transcript, on an explicit operator action with its own log
line. `InMemoryStore` implements all of them over its own dicts, which is how
tests stay offline and a no-Supabase dev box keeps working.

**What the dashboard shows**, per tenant over a 7/30/90-day window: headline tiles
(conversations by channel, bookings, booking rate, escalations, call minutes,
**Vapi telephony cost** from `sum(calls.cost_usd)`); conversations/day and
bookings/day series; breakdowns by service, `ended_reason` (the honest "did the
call work" signal) and escalation reason; recent activity lists opening to detail
views; and a **config health** panel that costs nothing because we already hold the
config — is `booking.provider` live or stub, is `notifications.provider` still
`"stub"`, is `vapi.assistant_id` set, is `chat.allowed_origins` empty, is
`allow_warm_transfer` off, are any MCP servers enabled.

**What it must not show.** There is no per-tenant LLM cost, no per-turn latency, no
conversation-outcome field, and no in-progress calls (`webhooks.py` discards
`status-update`). The risk isn't the absence — it's that showing *some* cost
invites the reader to believe it's *the* cost. **Label the tile "Vapi telephony
cost" and nothing broader.**

---

## Admin API + UI

### Routes

`/admin/api/tenants/{tenant_id}/…`, UI at `/admin`. The real reason for the shape
is that **the tenant id is in the path**, so the day tenant login lands the
authorization check is one comparison in one dependency covering every route.

```
GET  /admin/api/session                        who am I, which tenants, what may I do
GET  /admin/api/overview                       per-tenant rollup (operator landing)
GET  /admin/api/tenants                        list
GET  /admin/api/tenants/{tid}                  full TenantConfig + version + health flags
PUT  /admin/api/tenants/{tid}                  whole-document save (validated, versioned)
GET  /admin/api/tenants/{tid}/metrics?from&to  TenantMetrics + daily series
GET  /admin/api/tenants/{tid}/calls            CallSummary[] — no transcript
GET  /admin/api/tenants/{tid}/calls/{call_id}  full Call
GET  /admin/api/tenants/{tid}/chats            ChatSession[]
GET  /admin/api/tenants/{tid}/chats/{sid}      ChatMessage[]
GET  /admin/api/tenants/{tid}/jobs?since&until Job[]  (alist_jobs already windows)
GET  /admin/api/tenants/{tid}/escalations      Escalation[]
```

### Same-origin, not a CORS widening

`app/main.py` sets `allow_methods=["GET","POST","OPTIONS"]`, so a cross-origin
`PUT` is refused at preflight — **the endpoint never runs and the log shows a
clean 200 on the OPTIONS**. It looks like a UI bug and is a config one. Adding
`PUT`/`PATCH`/`DELETE` is rejected: `allow_origins=["*"]` is safe today *because*
the surface is GET/POST with `allow_credentials=False`; widening the method set on
a wildcard origin extends a config-mutating surface to every origin on the
internet, defended by a bearer alone. **Serve the admin UI same-origin from the
same app** — no CORS involved, `allow_methods` never changes. Add a comment in
`main.py` recording that `/admin/api` is deliberately same-origin-only.

### Auth: a new token, failing closed

**New `ADMIN_AUTH_TOKEN`, not `API_AUTH_TOKEN`.** The latter is the *trusted chat
caller* token held by `chat_cli`, tests and server-to-server callers, and its power
is "run a conversation as any tenant". Reusing it would silently promote every
holder to "can rewrite any tenant's escalation phone, Cal.com event type and voice,
and read every transcript" — a privilege escalation performed by a config change,
with no code review attached, on the wrong rotation schedule.

**`app/channels/admin_auth.py`** carries the abstraction the whole later-flip bet
rests on:

```python
@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    kind: Literal["operator", "tenant"]
    tenant_ids: tuple[str, ...] | None      # None == every tenant
    subject: str                            # "operator" today; a user id later
    def may_access(self, tenant_id: str) -> bool
    def may_write(self, tenant_id: str) -> bool

async def require_admin(...) -> AdminPrincipal
async def require_tenant_access(tenant_id: str, p = Depends(require_admin)) -> AdminPrincipal
```

Today `require_admin` has one branch: bearer → `operator`. Tomorrow it gains a
second: verified Supabase Auth JWT → `kind="tenant"`. Every route already depends
on `require_tenant_access`, so nothing else moves. ~40 lines now.

**Fails closed — the one deliberate break with house convention.**
`require_chat_caller` fails open because an unauthenticated dev chat costs a Groq
request; `require_admin` failing open means anyone who reaches the box rewrites
every tenant and reads every transcript. Unset `ADMIN_AUTH_TOKEN` → 401 on every
admin request, and `ADMIN_ENABLED=false` by default so the router isn't mounted at
all (404, not 401) on a box that hasn't opted in — making "is admin exposed?" a
grep for one env var rather than an inference.

**Preflight additions** (`app/preflight.py`), fatal under `APP_ENV=production`:
`admin_enabled` with no `admin_auth_token`; `admin_auth_token` shorter than 32
chars; `tenant_source == "supabase"` with no `supabase_url`; and — the
phantom-edit guard, the check that stops the worst Phase 8 outcome ever shipping —
**`admin_enabled` with `tenant_source == "json"`**, with the full explanation in
the message.

**Rate limiting.** `ratelimit.py` exempts secret-holding callers by design, but
`/admin/api/overview` fans out N round trips and the analytics endpoints query the
same free-tier project that answers phone calls. So: a generous per-IP limiter on
`/admin/api/*` (`admin_requests_per_minute: int = 120`) reusing `ratelimit._hit`
with a new scope as a router-level dependency; a **~30s cache on
`/admin/api/overview`** (more valuable than the limiter); and a **failed-auth
throttle** — `compare_digest` removes the timing oracle but not unlimited guessing.

### The UI

`widget/vite.config.ts`'s `emptyOutDir: true` + `fileName: () => "widget.js"` +
single `lib.entry` settles one thing: the admin app **cannot** be a second entry
there without breaking the frozen bundle filename or having the two builds delete
each other's output. It's a separate build or no build.

**Second Vite app under `admin/`.** The case for the alternative — one no-build
HTML file served by `FileResponse`, as `/widget/demo` already is — is real: no npm
surface, no second `node_modules`, no second committed artifact, no second
buildhash guard, and it can never go stale. If this were operator-only forever I'd
take it. Two things kill it: **it becomes customer-facing the moment tenant login
lands**, and the config editor is not a small form — `TenantConfig` is ten nested
models including a services list with add/remove, a seven-day hours grid with
per-day "closed" toggles, an MCP server list, and a keywords array. Hand-rolled
DOM for that, untyped against the model, is a 2,000-line file nobody touches in six
months.

The admin app **isn't an embed contract**, so it needs no library mode: plain
`vite build` with an `index.html` entry, hashed assets, `outDir: dist`. Delivery is
"serve a directory" — **the first `StaticFiles` mount in this codebase**. Two
gotchas, neither obvious:

- **Mount ordering.** A `StaticFiles` mount at `/admin` shadows `/admin/api/*` and
  returns HTML to a JSON client. Mount assets at `/admin/assets`, include the API
  router first, put the SPA catch-all (`GET /admin/{path:path}`) last. No test
  catches this unless one is written.
- **`StaticFiles(directory=…)` raises at mount time if the directory is missing**,
  turning "forgot to run `npm --prefix admin run build`" into a crashed boot — the
  opposite of `/widget.js`'s deliberate 404-with-a-pointer. Guard with
  `if ADMIN_DIST_DIR.is_dir()` and add `admin: "missing"` to `/health`'s
  `problems[]`, the pattern `widget: "missing"` already set.

**Stack:** Preact + TypeScript (same as `widget/`, so conventions transfer), a
~30-line hash router, and **no chart library** — bars and lines over ≤90 daily
points is ~80 lines of inline SVG. Plumbing mirrors `widget/` exactly:
`admin/scripts/buildhash.mjs` and `tests/test_admin_bundle.py` copied across.
**Repeat the Windows gotcha in both** — sort the relative-path *string*, not a
`pathlib.Path`; pathlib compares case-insensitively on Windows, Node's `sort()`
doesn't, and the two produce different digests over identical bytes.

---

## The "designed for tenant login later" contract

Seven decisions make the later flip additive rather than a rewrite:

1. **`AdminPrincipal` + `require_admin`/`require_tenant_access` from day one**,
   with every route depending on the latter and none on the raw token.
2. **Tenant id in the URL path** — authorization is one comparison in one place.
3. **Every analytics read goes through the tenant-scoped JWT** (`tenant_jwt`,
   `security_invoker` views, an RPC deriving its tenant from the JWT not a
   parameter). A logged-in tenant reading its own metrics uses the *same code
   path*; only who decided the tenant id changes. Had Phase 8 used the secret key
   "because operator mode sees everything", the flip would mean rewriting the
   analytics layer with RLS in mind. Failure mode avoided: **the admin key that
   never got removed.**
4. **`GET /admin/api/session` exists now**, returning `{kind, tenant_ids,
   capabilities}`; the UI branches on that response, never on a hardcoded "am I
   operator". The tenant build is the same bundle with a different session response.
5. **Reads and writes separated at the module level** — reads use the tenant JWT,
   writes use the secret key in `app/tenancy/admin.py`. When login lands only that
   module needs a tenant-scoped write variant, and phase10 item 12 already
   documents the RPC pattern.
6. **`_OPERATOR_ONLY_PATHS` ships now, inert.** Not every field is safe for a
   tenant to edit even with perfect auth: `tenant_id`, `phone_numbers`,
   `widget_keys`, `status`, `vapi.*`, `booking.event_type_id`, `voice.voice_id`,
   `mcp_servers` (SSRF — phase10 item 12). Enforce against `AdminPrincipal.kind`
   today, when every principal satisfies it. Ten minutes now versus auditing forty
   fields later under time pressure and missing one. **The highest-value cheap
   decision in the phase.**
7. **Do not grant the analytics views to `authenticated`.** `0002_rls.sql:6-11`
   chose `app_backend` specifically so customer-facing Supabase Auth users would
   not inherit backend grants. When login lands the browser talks to `/admin/api`,
   which mints an `app_backend` JWT server-side — it never talks to PostgREST. Say
   so in the migration header, because "just let the browser use supabase-js with
   the user's JWT" is the shortcut that blows the grant model open in one line.

---

## Implementation

Each step ends green on `pytest` and `ruff check .`.

**Step 0 — baseline.** Record the test count; `ruff check .`;
confirm `widget/dist/.buildhash` matches (`tests/test_widget_bundle.py` **skips
silently** when `dist/` is absent, so green is not proof). Confirm all seven
migrations are applied live — Phase 7 Step 10 re-created the project and applied
`0001`→`0007`; CLAUDE.md's older "Next" line predates that.

**Step 1 — `SupabaseTenantRepository` and the read-path flip.** New
`app/tenancy/supabase_repository.py`. Modified: `app/main.py` (lifespan wiring;
`tenant_source` in `/health`'s authenticated detail; a degraded-snapshot
`problems[]` entry), `app/preflight.py`, `app/config.py`
(`tenant_snapshot_refresh_seconds`), **`tests/conftest.py`**
(`loader.set_repository(None)` in `isolated_runtime` — the prerequisite above),
`.env.example`, `content/README.md`. New test:
`tests/test_supabase_tenant_repository.py`.

**Step 2 — analytics schema.** New `app/db/migrations/0008_analytics.sql` (five
views, one RPC, four indexes). Modified `tests/test_migrations.py` (the
`create view` lint).

**Step 3 — analytics store methods.** Modified `app/db/store.py` (`AnalyticsStore`
protocol, `alist_chat_sessions` on `ChatLog`), `app/db/models.py`
(`TenantMetrics`, `DailyMetrics`, `CallSummary`), `app/db/memory_store.py`,
`app/db/supabase_store.py`. New test: `tests/test_analytics_store.py`.

**Step 4 — admin auth and principal.** New `app/channels/admin_auth.py`. Modified
`app/config.py` (`admin_enabled`, `admin_auth_token`, `admin_requests_per_minute`),
`app/preflight.py`, `app/channels/ratelimit.py` (`enforce_admin_rate_limit` + the
failed-auth throttle), `.env.example`. New test: `tests/test_admin_auth.py`.

**Step 5 — admin read API.** New `app/channels/admin.py`. Modified `app/main.py`
(conditional `include_router`). New test: `tests/test_admin_api.py`.

**Step 6 — admin write path.** New `app/tenancy/admin.py`, new
`app/db/migrations/0009_admin.sql` (`tenants.updated_at`, the voice-consent trigger
fix). Modified `app/tenancy/sync.py` (`updated_at`, delete-of-absent-children),
`app/channels/admin.py` (the PUT route), `scripts/sync_tenants.py`
(`--force`, `--export`). New test: `tests/test_admin_write.py`; updated
`tests/test_tenant_sync.py`.

**Step 7 — the `admin/` UI.** New `admin/{package.json,vite.config.ts,tsconfig.json,index.html,README.md}`,
`admin/src/{main.tsx,App.tsx,api.ts,router.ts,styles.css}`,
`admin/src/views/{Overview,Tenant,Config,Calls,Chats}.tsx`, `admin/src/charts/`,
`admin/scripts/buildhash.mjs`. Modified `.gitignore`, `.dockerignore`. New test:
`tests/test_admin_bundle.py`.

**Step 8 — serve it.** Modified `app/main.py` (the guarded `StaticFiles` mount, the
SPA catch-all ordered last, `/health`'s `admin` field), `infra/Dockerfile`
(`COPY admin/dist`). Updated `tests/test_deploy_config.py`, `tests/test_api.py`.

**Step 9 — docs.** `CLAUDE.md` (Phase 8 done; new gotchas: the phantom edit, the
sync stomp, the voice-consent trigger's INSERT pass, `security_invoker` on views,
admin auth failing *closed* unlike everything else, `StaticFiles` raising at mount,
a second committed build artifact). `README.md` (status + an Admin section).
`content/README.md` — **"the JSON is now a seed"**, the most important doc change
in the phase, because someone will otherwise "fix" the drift by running
`sync_tenants`. `admin/README.md`, `infra/README.md` (`ADMIN_*` variables),
`.env.example`. `plans/phase10.md`: append the two entries below, rewrite item 8 as
done-in-Phase-8, and correct the closing "Avatar … is Phase 8" sentence. An
amendment note on plan §12 recording that Tavus-via-Vapi is discontinued.

**Step 10 — live verification.** The checklist below.

---

## Testing

Offline, on the existing `hermetic_settings` / `no_network` / `isolated_runtime` /
`mock_http` / `override_tenant` fixtures.

**`tests/test_supabase_tenant_repository.py`** — **round-trip fidelity**:
`_tenant_row(hotel)` + `_service_rows(hotel)` fed back through hydration yields a
`TenantConfig` equal to `hotel`. Both halves are pure functions, so it's free — and
it is the single most valuable test in the phase, because it's what silently breaks
if anyone edits `_TENANT_COLUMNS`. Plus: a malformed blob is skipped and logged and
that tenant falls back to JSON while others load; total failure serves JSON and
sets `degraded`; `find_by_phone` normalises digits identically to the JSON
repository (parametrised over both); `find_by_assistant_id` reads through
`config → vapi → assistant_id`; `refresh()` picks up a changed row.

**`tests/test_analytics_store.py`** — `InMemoryStore` aggregation across three
days; a `cancelled` job excluded from bookings; escalations grouped by reason.
**`CallSummary` has no `transcript` attribute** — an explicit assert, so it fails
loudly the day someone adds it back. `SupabaseStore` variants over `mock_http`: the
path is the *view*; `tenant_id=eq.X` is on the query string even though RLS also
enforces it (convention #3); and the `Authorization` header carries a **tenant JWT,
not the secret key** — the mechanical guard on the tenant-login contract.

**`tests/test_admin_auth.py`** — no `ADMIN_AUTH_TOKEN` → **401 everywhere** (with a
comment: deliberate break with fail-open); `ADMIN_ENABLED=false` → 404;
**`API_AUTH_TOKEN` presented to an admin route → 401** (the privilege-separation
regression guard); an operator reaches any tenant while a synthetic
`kind="tenant"` principal with `tenant_ids=("a",)` gets 403 on `b` — testing the
future path today; the failed-auth throttle 429s.

**`tests/test_admin_api.py`** — every read route against a stubbed store; the
calls-list response body contains no `transcript` key (assert on the JSON, not the
model); `/admin/api/overview` degrades per-tenant rather than failing the page;
**route ordering** — `GET /admin/api/tenants` returns JSON, not SPA HTML.

**`tests/test_admin_write.py`** — a valid PUT produces exactly the rows
`_tenant_row`/`_service_rows` would; **parametrised 422s with field paths** for
every existing validator (bad timezone, unknown weekday, duplicate slugs,
calcom-without-event-type, `close <= open`, illegal MCP name, `voice.speed = 3.0`,
`duration_minutes = 600`) — where "Pydantic is the validation layer" gets proven
rather than asserted; an operator-only path in a tenant principal's payload → 403;
`voice.voice_id` with no consent → **409 with actionable copy**, tested via both
the pre-check and a simulated `P0001` body; a removed service produces a DELETE for
the orphan; a stale version → 409; after a save, `clear_tenant_cache()` ran and the
repository refreshed (spy).

**`tests/test_admin_bundle.py`** — clone of the widget guard. Same caveat: it
**skips** when `admin/dist/.buildhash` is absent. There are now two artifacts with
that property.

**Updated:** `tests/test_migrations.py` (view lint), `tests/test_api.py` (`/health`
gains `admin`/`tenant_source`), `tests/test_deploy_config.py`,
`tests/test_preflight.py` (four new fatal cases), `tests/conftest.py`.

### Live verification, in order

1. Apply `0008` + `0009`; confirm each view exists and its `reloptions` shows
   `security_invoker=true`.
2. **The isolation proof, and the one that matters.** Mint a `northside-plumbing`
   tenant JWT by hand and `GET /rest/v1/daily_call_stats` — only northside rows,
   zero hotel-mzv. Repeat with the **anon** key: empty/403. A view leaking here is
   Phase 8's equivalent of Phase 4's cross-tenant-read check.
3. `TENANT_SOURCE=supabase` locally: `/health` reports `tenant_source: "supabase"`,
   `problems: []`, and `chat_cli --tenant hotel-mzv` answers using the **Supabase**
   greeting — proven by changing it in the DB, not the JSON.
4. Break it on purpose: point `SUPABASE_URL` at an unreachable host and restart.
   The boot **succeeds**, serves JSON fallback, `/health` names it in `problems[]`.
5. `ADMIN_ENABLED=true` with a real token: `/admin` loads, `/admin/api/session`
   returns `kind: "operator"`, every route 401s with no bearer *and* with
   `API_AUTH_TOKEN`.
6. **Edit hotel-mzv's greeting in the panel → the very next `chat_cli` turn uses
   it, no restart, no redeploy.** The acceptance criterion for the admin half.
7. Submit a deliberately invalid edit (timezone `Mars/Phobos`, duplicate slug,
   `close` before `open`) → 422, each error attached to the right input.
8. Set `voice.voice_id` with no consent row → **409 naming the `onboard_tenant`
   command**, not a 500. Insert a consent row; confirm it then succeeds.
9. **The trigger landmine.** On a tenant that already has a `voice_id` *and* a
   consent row, save an unrelated field. If rejected, `0009`'s fix is mandatory.
10. Remove a service in the panel; confirm the row is gone from `public.services`
    and `check_availability` no longer offers it.
11. Numbers against ground truth: three real chat turns and one real call, then
    confirm counts move by exactly the right amount and minutes match Vapi's own
    dashboard.
12. Open a call detail; confirm the transcript appears **only** there, and confirm
    with `curl` (not the UI) that the list endpoint carries none.
13. `sync_tenants.py` without `--force` refuses under `TENANT_SOURCE=supabase`;
    `--export` writes live config back to `content/tenants/*.json` and `git diff`
    shows the panel's edit.
14. `docker build -f infra/Dockerfile .`; the container serves `/admin` and
    `/admin/assets/*`.
15. Railway redeploy with `ADMIN_ENABLED=true`; preflight passes, and removing
    `ADMIN_AUTH_TOKEN` fails the boot naming it.
16. Hammer `/admin/api/overview` past the ceiling → 429 with `Retry-After`; normal
    use never trips it.

---

## Risks

1. **The phantom edit.** Admin panel + `TENANT_SOURCE=json` in production means
   writes that reach Postgres and never reach the bot, returning 200 every time.
   Both halves work perfectly in isolation, so nothing errors and nothing logs. The
   preflight check is the only thing that would ever catch it.
2. **The sync stomp.** `scripts/sync_tenants.py` blind-upserts the whole `config`
   blob and silently reverts every panel edit. `--force`/`--export` mitigate, but
   committed JSON and live config now genuinely diverge — so someone will
   eventually "fix" the drift by running sync. As much a `content/README.md`
   problem as a code one.
3. **The voice-consent trigger fires on an unrelated save.** The BEFORE INSERT pass
   of `INSERT … ON CONFLICT DO UPDATE` sees `old = null`, so any tenant with an
   existing `voice_id` may be rejected for editing its greeting. Invisible today
   only because no tenant has ever been cloned. ⚠️ VERIFY live (item 9).
4. **A `public` view without `security_invoker` is a silent cross-tenant leak**
   readable with the anon key, and the migration lint as written would not notice.
   The lint extension is mitigation, not a nicety.
5. **Cache invalidation is per-replica.** At two replicas half the traffic serves
   stale config for up to 300s with no error anywhere — and the operator notices
   first. Fourth reason for the single-replica constraint.
6. **The admin bearer has the largest blast radius of any secret in the repo** —
   every transcript readable, every tenant's config writable including
   `emergency.escalation_phone` (redirect emergencies) and `booking.event_type_id`
   (redirect bookings to another calendar). Phase 8 ships a minimum-length preflight
   check, a short holder list and rotation; real per-user auth is phase10 item 14.
7. **Cold boot now depends on Supabase.** The JSON fallback keeps it non-fatal, but
   a *partial* failure — Supabase reachable, one blob corrupt — silently runs that
   tenant on committed JSON that may be months old. Right answer, invisible
   provenance; the per-tenant WARNING and `/health` flag are the only signal.
8. **Analytics competes with the booking path** for the same PostgREST pool and the
   same free-tier project. A dashboard left open on auto-refresh is background load
   on the database that answers phone calls. No auto-refresh by default, a 30s
   overview cache, and the admin limiter.
9. **The dashboard cannot show what nobody records** — no per-tenant LLM cost, no
   per-turn latency, no outcome field, no in-progress calls. The risk isn't the
   absence; it's that showing *some* cost implies it's *the* cost.
10. **A second committed build artifact.** `admin/dist` joins `widget/dist` as
    something that goes stale silently, guarded by a test that **skips** when it's
    missing. A fresh clone has a green suite and a 404 admin panel.
11. **`StaticFiles` raises at mount time** if `admin/dist` is absent, turning a
    forgotten build into a crashed boot. Guard the mount.
12. **Route shadowing.** A catch-all registered before the API router returns HTML
    to JSON clients. Ordering is load-bearing and silent; test it.
13. ⚠️ **PostgREST error mapping is version-dependent** — whether an unqualified
    `raise exception` surfaces as 400 or 500 decides whether the voice-consent
    handler fires. The pre-check exists partly so the good message doesn't depend
    on it.

## Deferred

Cross-replica cache invalidation (LISTEN/NOTIFY or Realtime) → phase10, beside the
Redis rate limiter, same root cause. A `security definer` operator rollup RPC once
the per-tenant loop hurts. `status-update` webhook handling and a live-calls tile.
Audit logging of admin writes — worth doing, but it needs the per-user identity
only tenant login provides, so it belongs with item 14. Per-tenant LLM cost, which
needs a per-turn cost row that doesn't exist and a `metrics.py` rewrite its own
docstring warns off.

## Est. effort

4–5 days. Steps 1–3 are the substance and roughly half the time (the read-path flip
and the analytics schema each carry a live-verification pass of their own). Step 6
is where the surprises live — the trigger interaction and delete-of-absent-children
are both things you find by running them. Step 7 is a day of UI that expands to two
if the config editor is done properly, which it should be, because that's the part
that becomes customer-facing.

---

# Appendix — the two `plans/phase10.md` entries (Step 9)

Drop-in prose matching the existing entry format. **13** goes in *"Needs an
external input from you"*; **14** in *"No external blocker"*. Item 8 is rewritten
in place as done-in-Phase-8, and the closing "Avatar … is Phase 8" sentence
corrected.

### 13. The video avatar (moved out of Phase 8)

Phase 8 shipped analytics and per-tenant admin; the avatar was deliberately left
out — you want it, later. Two things changed since plan §12 was written.

**§12 is stale on the key point.** It recommends *"Tavus (already integrated with
Vapi)"* — **Vapi discontinued its Tavus integration** (Vapi staff, 20 Jun 2025).
There is no longer a provider-side toggle that adds a face to a call. The workable
path is now **browser-side**: Vapi's web SDK exposes a public
`getDailyCallObject()` and emits a `'video'` event carrying a `MediaStreamTrack`,
and **Simli** mints a short-lived session token server-side (`POST /compose/token`,
the startAudioToVideoSession call, from an API key plus a `faceId`) which the
browser then uses over WebRTC. The avatar is a composition the *client* performs,
not an add-on the orchestrator provides. Cost ≈ **$0.05/min**, free tier $10
signup credit + 50 min/month — enough to prove the path without a commitment.

**A familiar trap is already in the repo.** `.env.example:258-261` has carried
`TAVUS_API_KEY=` / `SIMLI_API_KEY=` placeholders since Phase 0, but neither is a
real `Settings` field and `model_config` has `extra="ignore"` — so they are
silently dropped exactly the way `LANGCHAIN_*` was for six phases (CLAUDE.md's
tracing gotcha). Making `simli_api_key` a real field is the first line of work.

You want both delivery modes, and they're genuinely different products:

- **Avatar mode inside the existing chat widget**, gated per tenant and toggleable
  from the Phase 8 admin panel. A new `TenantConfig.avatar` sub-model (`enabled`,
  `provider: Literal["simli"]`, `face_id`, `mode: Literal["widget","embed","both"]`),
  surfaced in `/chat/session`'s response so the widget knows whether to render the
  button, and rendered as a form section in `admin/src/views/Config.tsx`. The frozen
  `<script data-widget-key>` contract is untouched — the widget just learns a new
  capability from the handshake it already performs.
- **A separate avatar-only embed/demo page** for users who should get voice/avatar
  and no text chat. A full page, not an embed, so it can't live in `widget/`'s
  library-mode IIFE bundle. Ride it on the build system Phase 8's `admin/` app
  established rather than adding a third toolchain.

Server-side, one new endpoint: **`POST /avatar/session`**, minting the Simli token
with `SIMLI_API_KEY` **server-side only** (never shipped to the browser),
authenticated by the same widget session token `/chat` already accepts, refusing
when `tenant.avatar.enabled` is false, and rate-limited — per-minute billing on a
public endpoint is a spend hole. **The brain is untouched throughout** (convention
#4): a presentation layer over the same graph, voice and tools.

**What's needed:** a Simli account and API key, plus a chosen `faceId` (their
library, or a custom face — which has its own upload + likeness-consent step, and
convention #6's reasoning about voice applies just as squarely to a face). Also a
decision on which tenants get it, since it's metered per minute and is the obvious
paid add-on.

**Then:**
```
# 1. make the key a real Settings field, add TenantConfig.avatar
# 2. POST /avatar/session mints the Simli token server-side
# 3. widget: getDailyCallObject() -> 'video' track -> Simli WebRTC composition
# 4. toggle it on for one tenant from /admin, confirm the button appears
python -m scripts.chat_cli --tenant <id>   # unchanged: the brain never learns an avatar exists
```
⚠️ VERIFY before building: the exact Simli token endpoint path and payload shape;
whether `getDailyCallObject()` is stable across the Vapi web SDK version in use;
and whether the `'video'` event's track is already audio-driven or needs a separate
audio tap to feed Simli. All three read fine in docs and behave differently in a
browser.

Effort: medium — 2–3 days, most of it in the browser and most of *that* getting two
WebRTC sessions to agree on timing. The server side is one endpoint and one config
field.

### 14. Real per-tenant login (Supabase Auth)

Phase 8 shipped the admin surface "operator-only now, designed for tenant login
later", and the *later* half is genuinely pre-built rather than merely promised.
Already in place: `AdminPrincipal` and `require_admin`/`require_tenant_access`
(`app/channels/admin_auth.py`), with every admin route depending on the latter and
none on the raw token; the tenant id in the URL path, so authorization is one
comparison in one dependency; `GET /admin/api/session` returning
`{kind, tenant_ids, capabilities}` that the UI already branches on;
`_OPERATOR_ONLY_PATHS` in `app/tenancy/admin.py`, enforced today against a set every
current principal satisfies; and — the load-bearing one — **every analytics read
already goes through the tenant-scoped JWT** (`app/db/auth.py::tenant_jwt`,
`security_invoker` views, an RPC deriving its tenant from the JWT rather than a
parameter), so a logged-in tenant reading its own metrics runs the identical code
path an operator does.

What's missing is one branch in `require_admin` and a user→tenant mapping.

The mapping is the only real design question: **a GoTrue JWT carries no `tenant_id`
claim by default.** Two options — a custom access-token hook injecting the claim
(Supabase-version-dependent, ⚠️ VERIFY availability on the project's plan), or an
`admin_users (user_id uuid, tenant_id text, role text)` table the backend reads.
**Prefer the table**: it's a migration in this repo rather than a dashboard setting
nobody can diff, it supports one user administering several tenants, and it makes
the operator/tenant distinction a row rather than a magic claim.

One rule is non-negotiable and is why this stays a backend concern: **the browser
must never hold a JWT that PostgREST accepts.** It talks to `/admin/api`; the
backend verifies the GoTrue token (verify, don't decode — `widget_auth.py` is the
only verification code in the repo today and it's HMAC; GoTrue signs HS256 against
`SUPABASE_JWT_SECRET` on legacy projects and ES256/JWKS on newer ones, ⚠️ VERIFY
which applies) and then mints its own `app_backend` tenant JWT for the data reads.
That is exactly what `0002_rls.sql:6-11` chose `app_backend` over `authenticated`
to preserve. Letting the browser use supabase-js with the user's own token is the
one-line shortcut that undoes it.

**What's needed:** a decision that tenants get logins at all (it changes the product
from "we run it for you" to "you have an account"), Supabase Auth enabled on the
project, and a call on invite-only vs. self-serve signup — the former is right for
a handful of pilot clients and avoids a whole email-verification surface.

**Then:**
```
# 1. 00NN_admin_users.sql — user_id -> tenant_id mapping, RLS'd like everything else
# 2. require_admin gains a second branch: verified GoTrue JWT -> AdminPrincipal(kind="tenant")
# 3. admin/src: a login view; the rest of the UI already branches on /admin/api/session
# 4. _OPERATOR_ONLY_PATHS stops being inert — no route changes needed
```
Landing this also unblocks **item 12** (self-serve MCP server registration), which
was deferred specifically for want of a tenant-facing auth surface — and it
inherits that item's two documented risks unchanged: `set_tenant_secret` needs a
tenant-scoped write variant deriving `tenant_id` from the JWT the way
`get_tenant_secret` already does, and a tenant-submitted server URL is an SSRF
vector that doesn't exist while only a trusted operator types them.

Effort: medium — 2 days for auth plus the mapping table, another for the login UI
and session handling. Small precisely because Phase 8 paid the design cost up
front; it would be a rewrite otherwise.

---

## Critical files

| Path | Why it matters |
|---|---|
| `app/tenancy/loader.py` | the `set_repository` / `clear_tenant_cache` seam Steps 1 and 6 both pivot on |
| `app/tenancy/sync.py` | the existing admin write path (`_TENANT_COLUMNS`, `_tenant_row`, `sync_tenant`) that `app/tenancy/admin.py` extends, and the definition of the `config` JSONB round-trip |
| `app/tenancy/repository.py` | the read-only protocol being implemented a second time; `invalidate()` at :84 |
| `app/db/supabase_store.py` | the tenant-JWT read pattern every analytics method must copy |
| `app/main.py` | lifespan wiring, CORS `allow_methods`, `/health`, and the first `StaticFiles` mount + route ordering |
| `app/db/migrations/0002_rls.sql` | the RLS/GRANT pattern `0008`'s views must reproduce, and the `app_backend`-not-`authenticated` decision the tenant-login contract rests on |
| `app/db/migrations/0005_voice_consent.sql` | the trigger whose INSERT-pass behaviour `0009` fixes |
| `tests/conftest.py` | `isolated_runtime` needs `loader.set_repository(None)` before Step 1's tests can be trusted |
| `widget/vite.config.ts`, `widget/scripts/buildhash.mjs` | the templates `admin/` mirrors, and the `emptyOutDir` constraint that forces a separate build |
