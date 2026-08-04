# infra — Phase 7 (Deploy + harden)

One service, one deploy target: Railway, Docker builder, always-on (no
scale-to-zero — Vapi times out while a container wakes and the caller
hears nothing).

## Building the image

```
docker build -f infra/Dockerfile .
```

Must build from the **repo root** (`-f infra/Dockerfile .`, not
`cd infra && docker build .`) — every `COPY` path in the Dockerfile is
root-relative.

Two stages:
- `deps` — installs the full dependency tree from `pyproject.toml`. Also the
  target used to regenerate the lockfile below.
- `runtime` (the default target) — installs from `infra/requirements.lock.txt`
  instead of re-resolving from `pyproject.toml`, so two builds of the same
  commit can't silently resolve different versions (this project has hit
  that failure mode twice — see CLAUDE.md's `uuid_utils` and `[google]`
  gotchas).

### Regenerating the lockfile

Whenever `pyproject.toml`'s dependencies change:

```
docker build -f infra/Dockerfile --target deps -t air-deps .
docker run --rm air-deps pip freeze | grep -vi '^ai[-_]receptionist' > infra/requirements.lock.txt
```

Must run **inside the Linux image**, never `pip freeze` from the Windows
dev `.venv` — that would bake in `pywin32` and the win32-only
`uuid-utils<0.16` cap. Diff the result against the dev venv for
`langchain-core`/`langgraph` before trusting it (a resolution drift here is
exactly the kind of thing worth a second look, not just patch-level noise).

## What ships in the image vs. what doesn't

`app/`, `scripts/`, `content/` (tenant configs + prompt — read from disk at
runtime, so it must ship), `widget/dist/widget.js` + `.buildhash`, and
`admin/dist/` (Phase 8) — all three built on the host (`npm --prefix widget
run build`, `npm --prefix admin run build`) and committed; no Node in this
image. `tests/` and `.venv/` never ship — see `.dockerignore`.

## Required environment (platform secrets, never baked into the image)

Every var in `.env.example`. Below production preflight (`app/preflight.py`)
refuses to boot without: `API_AUTH_TOKEN`, `VAPI_WEBHOOK_SECRET`,
`WIDGET_SESSION_SECRET`, `PUBLIC_BASE_URL` (must be `https://`),
`SUPABASE_JWT_SECRET` (if `SUPABASE_URL` is set), and an API key for the
active `LLM_PROVIDER`. `DATABASE_URL` unset stays a *warning* — the
checkpointer degrades to in-memory rather than failing the boot; check
`/health`'s `problems` field for that, not the preflight.

**Admin dashboard (Phase 8), if you're turning it on:** `ADMIN_ENABLED=true`
needs `ADMIN_AUTH_TOKEN` (32+ chars — preflight refuses anything shorter)
**and** `TENANT_SOURCE=supabase`, both fatal if missing. The `TENANT_SOURCE`
requirement isn't a style preference: an admin panel editing config while
the app still reads `content/tenants/*.json` produces edits that reach
Postgres and never reach the running bot ("the phantom edit" —
`plans/phase8.md`). Set `ADMIN_ENABLED=false` (the default) on any
deployment that doesn't need the surface — that's what keeps the whole admin
API 404ing rather than needing its own network-level lockdown.

`DATABASE_URL` must be the Supavisor **session-mode pooler**,
`aws-0-<region>.pooler.supabase.com:5432` — never the direct
`db.<ref>.supabase.co` host (IPv6-only since Jan 2024) and never port 6543
(transaction mode breaks the checkpointer's prepared statements).

## Region

**Co-locate the app with Groq and Vapi (both US-based)** — cross-region hops
eat into the 600–800ms end-to-end budget (plan §13). If Supabase is in a
different region than the app, `app/brain/runner.py`'s cold-thread
checkpointer read adds that round trip to *every* voice turn's first-token
latency, not just booking calls — see `plans/phase7.md`'s "region
trade-off" note for the full accounting and the options if that split can't
be avoided.

## Deploying (Railway)

1. Docker builder, `dockerfilePath: infra/Dockerfile`, `healthcheckPath:
   /health`, **one replica** (see "Process model" below).
2. Reserve the domain **first**, set `PUBLIC_BASE_URL` to it.
3. From the dev box: `python -m scripts.provision_vapi --tenant <id>` —
   bakes `PUBLIC_BASE_URL` and `VAPI_WEBHOOK_SECRET` into the Vapi
   assistant's `model.url`/`server.url`/`model.headers`/`server.secret`.
4. Commit the `content/tenants/*.json` that step writes back (it adds
   `vapi.assistant_id`).
5. *Then* deploy. Getting steps 3–5 in the wrong order means the running
   image's tenant JSON has no assistant id, and `find_by_assistant_id`
   silently falls through — the caller hears the greeting (TTS, no LLM
   round trip) and then silence, while `/health` stays green.
6. Set `APP_ENV=production` — this is what turns on the preflight in step
   above; every secret must be real at this point.

## Removing a bot (Phase 9 Part B)

Two operations, deliberately different in cost — see `plans/phase9.md`
Risk 5 for the full reasoning.

**Archive** (`/admin` → tenant → Config tab → Danger Zone → Archive, or
`POST /admin/api/tenants/{id}/archive`) is a pure `status: "archived"` flip.
Every row survives. `resolve_tenant_id` (`app/tenancy/loader.py`) refuses to
serve an archived tenant on any channel — a Vapi call or a widget handshake
gets a clean refusal instead of an answer — but nothing is deleted, and
**Restore** undoes it instantly.

**Purge** (Danger Zone → type the tenant id to confirm → Purge permanently,
or `POST /admin/api/tenants/{id}/purge` with `{"tenant_id": "<same id>"}` as
body confirmation) is irreversible. Refused unless the tenant is already
archived. Deletes, in this exact FK order (`app/tenancy/admin.py::_PURGE_TABLES`
— most of these tables have no `on delete cascade` from `tenants`, so this
order is load-bearing, not stylistic):

```
chat_messages → chat_sessions → escalations → messages → jobs → calls
→ services → mcp_servers → voice_consents → tenants
```

Then, best-effort (logged on failure, never aborting the row deletion above
since that's already irreversible by that point): every Vault secret this
tenant has, the Vapi assistant if one was ever provisioned, and a committed
`content/tenants/<id>.json` if one exists. **Purge destroys every call
transcript and chat transcript for that tenant along with everything
else** — there is no separate export step, so pull anything worth keeping
(`GET /admin/api/tenants/{id}/calls/{call_id}`, `.../chats/{session_id}`)
before confirming.

**Manual SQL fallback**, if the panel is unavailable — run as the same
sequence, against the Supabase SQL editor or `psql`, substituting the real
tenant id:

```sql
delete from public.chat_messages where tenant_id = '<id>';
delete from public.chat_sessions where tenant_id = '<id>';
delete from public.escalations   where tenant_id = '<id>';
delete from public.messages      where tenant_id = '<id>';
delete from public.jobs          where tenant_id = '<id>';
delete from public.calls         where tenant_id = '<id>';
delete from public.services      where tenant_id = '<id>';
delete from public.mcp_servers   where tenant_id = '<id>';
delete from public.voice_consents where tenant_id = '<id>';
delete from public.tenants       where tenant_id = '<id>';
select public.delete_tenant_secrets('<id>');  -- Vault cleanup (0010_lifecycle.sql)
```

This bypasses the archived-status precondition and the typed confirmation —
both are panel/API-layer guards, not database constraints — so treat the
manual path with the same care the panel's confirmation step exists to
enforce. It does **not** delete the Vapi assistant or the committed JSON
file; do both by hand (`VapiClient.delete_assistant` /
`rm content/tenants/<id>.json` + a commit) if they exist.

## Process model — one worker, one replica, not a placeholder default

Three pieces of state are process-local with no cross-process sharing:
`app/brain/metrics.py`'s `TurnCounter`, `app/channels/widget_auth.py`'s
per-process fallback session secret (only matters if `WIDGET_SESSION_SECRET`
is unset — set it in production), and `app/channels/ratelimit.py`'s
in-process rate limiter. `--workers > 1` or a second Railway replica breaks
all three silently — the limiter gets too permissive, not too strict, which
is the more dangerous direction to get wrong.

## Health, readiness, and keep-alive

- `GET /health` — always 200, `status: "degraded"` with a `problems[]` list
  if something's off (checkpointer fell back to memory, widget bundle
  missing, MCP unavailable). `env`/`llm_provider`/`model`/the full tenant
  roster/`tracing` are gated behind the `API_AUTH_TOKEN` bearer — an
  anonymous caller gets the operational fields only.
- `GET /readyz` — actually touches the database. Point Railway's own
  healthcheck at `/health` (cheap, no DB round trip on every poll) and the
  keep-alive cron (`.github/workflows/keepalive.yml`, needs a `DEPLOY_URL`
  repo secret) at `/readyz` — free Supabase projects pause after 7 idle
  days, and `/health` alone would never touch the database often enough to
  prevent that.

## Rate limiting

In-process, per-replica, no new dependency (`app/channels/ratelimit.py`) —
a ceiling against one caller burning a tenant's LLM/Cal.com budget, not the
shared usage-tier model `plans/phase10.md` defers. Applied only to
`POST /chat` (widget-token callers only — a trusted `API_AUTH_TOKEN` caller
is exempt) and `POST /chat/session`. Never `/chat/completions` or
`/webhooks/vapi` — those are already gated by `VAPI_WEBHOOK_SECRET`, and
throttling a live phone call is worse than the spend.

## Tracing

`LANGCHAIN_TRACING_V2=true` + `LANGCHAIN_API_KEY` turns on LangSmith. Off by
default: chat/call transcripts carry guest names and phone numbers, so this
is a PII decision as much as an ops one. Check `/health`'s authenticated
`tracing` field to confirm it's actually active (both the flag and the key
must be set — the flag alone does nothing).

## Load/latency testing

```
python -m scripts.loadtest --base-url https://<your-deploy> --endpoint chat --concurrency 5 --turns 20
python -m scripts.loadtest --base-url https://<your-deploy> --endpoint voice --concurrency 1 --scenario booking --vapi-secret <VAPI_WEBHOOK_SECRET>
```

Reports p50/p95/p99 first-token and turn-total latency; pass/fail against
`FIRST_TOKEN_BUDGET_MS` (this app's own share of plan §13's budget) only
applies to `--endpoint voice`. Run `--concurrency 1` first if you want the
per-turn LLM request count to mean anything (`TurnCounter` is a
process-wide counter — it double-counts under concurrent turns).

## CI

`.github/workflows/ci.yml` — `ruff check .`, `ruff format --check .`,
`pytest`, and a `docker build -f infra/Dockerfile .` job, on every push/PR.
No Node/npm step: `tests/test_widget_bundle.py` already catches
`dist/`-vs-`src/` drift in Python (see `widget/README.md`).
