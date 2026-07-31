# Phase 6 — MCP layer

## Context

Phases 1–5 are done. The brain answers voice and chat, books against a real Cal.com
calendar, persists to Supabase behind real RLS, and ships an embeddable widget. Every
tool it can call is one of the five native tier-1 tools.

Plan §11 is the other half of CLAUDE.md convention #2: **the long tail**. A tenant should
be able to point the receptionist at any number of MCP servers — a CRM, a spreadsheet,
a web search, an internal API — and have those tools appear in conversation without a
code change. The acceptance criterion (§15) is *"a tenant's enabled MCP tools show up
and work in a conversation."*

The seam already exists and has since Phase 1: `app/brain/nodes/reason.py:44` calls
`await load_mcp_tools(tenant)` and appends the result to the native tools before
binding. `app/mcp/client.py` returns `[]`. `McpServerConfig` exists
(`app/tenancy/models.py:183`), the `mcp_servers` table exists with RLS and a grant
(`0001_schema.sql:149`, `0002_rls.sql:98-105`), and `MCP_ENABLED` / 
`MCP_TOOL_TIMEOUT_SECONDS` exist in `Settings` and `.env.example`. Phase 6 fills in
known shapes.

**One thing in the existing code is wrong, and it is the centre of this phase.**
`app/brain/graph.py:42` builds `ToolNode(NATIVE_TOOLS)` **once**, at compile time, from
a static list. `reason` binds MCP tools per tenant, so the model can and will emit a
call for a tool the `tools` node has never heard of — the call fails, the caller hears
a fallback line, and nothing in the logs says why. The comment at `graph.py:40-41`
flags it ("ToolNode will need rebuilding per tenant once the registry is dynamic")
without solving it. Step 6 is the fix.

## Decisions locked

| | |
|---|---|
| **Adapter** | `langchain-mcp-adapters` **>=0.3**, `MultiServerMCPClient`, as plan §11 specifies. Kept an **optional extra** (`pip install -e ".[mcp]"`) with a lazy import and a degrade-to-`[]` fallback — the `app/db/checkpointer.py` pattern, for the reason in Risk 1. |
| **Transports** | **HTTP only by default.** `MCP_ALLOW_STDIO=false` refuses any `transport: "stdio"` server with a loud warning. A `command` string from tenant config is arbitrary code execution on the one box holding every tenant's data, and a hosted deploy usually can't spawn processes anyway. The flag exists so a local stdio server is still reachable when an operator explicitly wants it. |
| **Registry read path** | **New `MCP_SOURCE` setting** (`"json"` \| `"supabase"`, default `"json"`), the same shape as `TENANT_SOURCE`. Dev and tests read `content/tenants/<id>.json`; production reads `public.mcp_servers`. This is the one place the tenant read path flips early, deliberately — see "Why the table, and why only here". |
| **Auth** | `${secret}` **placeholder substitution** into the server's `url` *and* its `headers`, resolved from Vault by reference (`auth_secret_ref`). Not an auth-style enum: Tavily's hosted server takes its key as a **URL query parameter** while most take a bearer header, and "connect literally any remote MCP server" means neither shape can be privileged. Default when `headers` is empty and a ref is set: `Authorization: Bearer <secret>`. |
| **Proving it** | A **first-party demo MCP server in-repo** (`scripts/demo_mcp_server.py`) — zero accounts, zero credentials, exercises the entire path, and doubles as the fixture the live check runs against. A third-party search/scraper server (Tavily / Firecrawl / Exa) is a **documented config example**, wired whenever a key exists; the loader is vendor-neutral so that is config, not code. |
| **Native tools stay native** | No tier-1 tool moves to MCP. Plan §11's latency discipline: `check_availability` / `book_job` / `send_confirmation` / `escalate` / `is_emergency` stay typed and in-process. |

### Why the table, and why only here

Phase 4 deferred the tenant *config* read-path flip for a specific, narrow reason:
`tests/conftest.py`'s `isolated_runtime` walks `get_repository().list_ids()` in an
autouse fixture, so pointing that at Supabase makes every test open a socket. That
reason **does not apply to the MCP registry**: `servers_for()` is called only from
inside `load_mcp_tools`, which is already async, already gated on `MCP_ENABLED` (off by
default), and already returns `[]` on any failure. No fixture touches it at collection
time.

So the risk profile is genuinely different: reading the table costs one extra request
on a path that is by definition the long tail, and a registry outage degrades to "no
MCP tools this turn" — never to a failed booking. That is what makes it safe to flip
here while `TENANT_SOURCE` stays deferred (`plans/phase10.md` item 8).

`scripts/sync_tenants.py` keeps pushing JSON `mcp_servers` into the table, so the two
sources agree and flipping `MCP_SOURCE` either way is non-destructive.

## What I need from you

**Nothing blocks Steps 0–9.** Everything below can be built, tested and demonstrated
with no accounts and no credentials.

Two things when you want them, neither blocking:

1. **A search/scraper API key** — Tavily (`tvly-…`), Firecrawl or Exa, whichever you
   end up preferring. It is one `register_mcp_server` command; the loader needs no
   change. Deferred to `plans/phase10.md` item 11.
2. **A live Supabase check on `MCP_SOURCE=supabase`** — requires `0007_mcp.sql` applied
   to the project, the same manual dashboard-SQL step every prior migration needed.
   Note `0006_chat.sql` is **still unapplied** (see CLAUDE.md "Current state"); apply
   both in one sitting.

---

## Implementation

Each step ends with a green `pytest`. Steps 1–6 and 8 need no credentials and no
network.

### Step 0 — baseline, and a dependency pre-flight

`pytest` → record the count. Then, **before touching `.venv`**, install the extra into a
throwaway environment and check what actually lands:

```
python -m venv %TEMP%\mcp-probe && %TEMP%\mcp-probe\Scripts\pip install "langchain-mcp-adapters>=0.3"
%TEMP%\mcp-probe\Scripts\python -c "import rpds, cryptography, jsonschema, win32api, mcp; print('all import ok')"
```

This is not ceremony. `mcp` 1.28.1 depends on `jsonschema` (→ **`rpds-py`**, a compiled
Rust extension), `pyjwt[crypto]` (→ **`cryptography`**, compiled — the exact dependency
Phase 4 designed around to avoid), and **`pywin32`** on win32. CLAUDE.md's first
environment gotcha is a compiled DLL (`uuid_utils` 0.16+) blocked by Windows
Application Control that broke *every import in the project*, not just its own. Finding
that out in a disposable venv costs two minutes; finding it out in `.venv` costs a day.

If the probe fails, the phase still ships — the code degrades by design (Step 5) — but
you develop against the fake loader and verify on Linux.

`pyproject.toml`: change `mcp = ["langchain-mcp-adapters>=0.1"]` to
**`>=0.3`**. 0.3.0 requires `langchain-core>=1.0,<2.0`; the current `>=0.1` would let
pip resolve to a 0.1.x release built against core 0.3.x and quietly downgrade the whole
stack — the failure mode CLAUDE.md already records for `pip install -e ".[google]"`.
This box is on core 1.5.0, so >=0.3 is the only correct pin.

### Step 1 — config and models (no behaviour change)

`app/config.py` — new **real `Settings` fields** (never ad-hoc `os.environ`;
`hermetic_settings` strips only names matching a field, the lesson from
`plans/phase3.md`), beside the two that already exist:

* `mcp_source: Literal["json", "supabase"] = "json"`
* `mcp_allow_stdio: bool = False`
* `mcp_tool_cache_ttl_seconds: int = 300` — mirrors `tenant_cache_ttl_seconds`
* `mcp_max_tools: int = 8` — hard cap on MCP tools bound per turn (Risk 3)
* `mcp_connect_timeout_seconds: float = 5.0` — distinct from the existing
  `mcp_tool_timeout_seconds`, which governs a tool *call*

`app/tenancy/models.py` — extend `McpServerConfig` (currently 8 lines, unused):

```python
name: str                      # + validator: ^[a-z0-9][a-z0-9_-]{0,31}$
enabled: bool = True
headers: dict[str, str] = {}   # values may contain ${secret}
tool_allowlist: list[str] = [] # empty = every tool the server offers
```

The **name validator is load-bearing, not cosmetic**: with `tool_name_prefix=True` the
server name becomes part of every tool name, and OpenAI/Groq/Gemini all reject a tool
name outside `^[a-zA-Z0-9_-]{1,64}$` — with a 400 on the *whole request*, so one server
called `"my server"` breaks every turn for that tenant, including booking. Fail at
config load instead.

Add a model validator: `transport == "http"` requires `url`; `transport == "stdio"`
requires `command`. Same "fail at config load, not mid-call" reasoning as
`_calcom_tenants_declare_event_types`.

`.env.example` — extend the existing MCP block with the new keys and a comment
explaining `MCP_SOURCE` (json in dev, supabase in production) and why
`MCP_ALLOW_STDIO` defaults off.

`GET /health` (`app/main.py:114`) — add `"mcp"`: `"off"` when `MCP_ENABLED` is false,
else `"json"` / `"supabase"` per `MCP_SOURCE`, or `"unavailable"` when the adapter
import fails. Same reasoning as the `store` / `widget` fields: a misconfigured deploy
is one `curl` away from visible.

### Step 2 — `app/db/migrations/0007_mcp.sql`

`ALTER TABLE public.mcp_servers` adding `enabled boolean not null default true`,
`headers jsonb not null default '{}'::jsonb`, `tool_allowlist text[] not null default
'{}'`. The table itself, its RLS, its `FORCE`, its `tenant_id` policy and its grant to
`app_backend` all already exist from `0001`/`0002` — this creates no table, so
`tests/test_migrations.py` (which keys on `create table public.X`) passes unchanged.

Header comment: this migration must be applied to the live project **together with the
still-unapplied `0006_chat.sql`**.

### Step 3 — the registry (`app/mcp/registry.py`, new)

```python
async def servers_for(tenant: TenantConfig) -> list[McpServerConfig]
```

Dispatches on `settings.mcp_source`. The `"json"` branch is `tenant.mcp_servers`
filtered to `enabled`. The `"supabase"` branch is one PostgREST GET —
`/mcp_servers?tenant_id=eq.<id>&enabled=is.true` — carrying the tenant-scoped JWT from
`app/db/auth.py`, exactly the pattern `SupabaseStore._request` uses
(`app/db/supabase_store.py:81-108`); reuse that helper's shape rather than a second
copy, including the "always filter by `tenant_id` explicitly, never rely on RLS alone"
rule at the top of that file.

**Any registry failure returns `[]` with a WARNING — it never raises.** MCP is the long
tail by definition; a Supabase blip must degrade to "no long-tail tools this turn", not
to a failed booking. This is deliberately the opposite posture to
`app/tenancy/secrets.py`'s vault handling, where an error must *not* be treated as
absent — the difference is that a missing MCP tool is a smaller capability, whereas a
missing secret means booking into the wrong tenant's calendar.

### Step 4 — connection building (`app/mcp/connections.py`, new)

`McpServerConfig` → the adapter's connection dict. Isolated in its own module for the
same reason `app/channels/vapi_schema.py` is: it's the only place that knows another
vendor's wire shape, so an adapter change is one file.

* `transport: "http"` → **`"streamable_http"`**. The adapter also accepts `"http"`, but
  emit the canonical value.
* `transport: "stdio"` → **skipped with a WARNING** unless `settings.mcp_allow_stdio`.
* Timeouts from `mcp_connect_timeout_seconds` / `mcp_tool_timeout_seconds`.
* **Secret substitution.** When `auth_secret_ref` is set, resolve it with
  `resolve_secret(tenant.tenant_id, ref, env_value=None)`
  (`app/tenancy/secrets.py:74`) and substitute `${secret}` in `url` and in every
  `headers` value. When `headers` is empty and a ref is set, default to
  `{"Authorization": f"Bearer {secret}"}`.
* A `TenantSecretError` **skips that one server** with a WARNING and keeps the others.
  Never fall back to connecting unauthenticated — that produces a confusing 401 loop
  instead of a clear "this server is misconfigured" line.
* **`redacted(connection)` helper**, mirroring `vapi_schema.redacted()`. Nothing may log
  a raw MCP URL: with query-parameter auth (Tavily's
  `?tavilyApiKey=tvly-…`) the URL *is* the credential, and it would otherwise appear in
  every connection log line and every traceback.

Two config examples belong in the docstring, because they're the two auth shapes in the
wild:

```jsonc
{ "name": "tavily", "transport": "http",
  "url": "https://mcp.tavily.com/mcp/?tavilyApiKey=${secret}",
  "auth_secret_ref": "TAVILY_API_KEY" }

{ "name": "acme_crm", "transport": "http", "url": "https://acme.example/mcp",
  "headers": { "Authorization": "Bearer ${secret}", "X-Acme-Org": "hotel-mzv" },
  "auth_secret_ref": "ACME_CRM_TOKEN" }
```

### Step 5 — the loader (`app/mcp/client.py`, rewrite)

Keeps its signature — `async def load_mcp_tools(tenant) -> list[BaseTool]` — so
`reason.py:44` is untouched. Gains `*, client=None` for test injection, matching
`CalcomBookingProvider` / `TwilioNotifier` / `SupabaseStore`.

* **Lazy import inside a `try/except ImportError`** → `[]` + one WARNING. The adapter is
  an optional extra whose dependency chain includes three compiled packages (Step 0);
  a blocked DLL must cost the long tail and nothing else. Identical posture to
  `init_postgres_checkpointer`.
* `MultiServerMCPClient(connections, tool_name_prefix=True, handle_tool_errors=True)`.
  `tool_name_prefix` defaults to **False** in 0.3.0 — plan §11's collision safety is
  opt-in, so set it explicitly. ⚠️ VERIFY the separator the installed version uses and
  confirm prefixed names still satisfy the provider name regex.
* Load **per server** via `get_tools(server_name=…)`, each wrapped in
  `asyncio.wait_for(mcp_connect_timeout_seconds)`, collecting what succeeds. One dead
  server must not stall a live call — this is the §13 budget on the line, and a
  third-party server is not something we control.
* Apply `tool_allowlist`, then truncate to `mcp_max_tools` with a WARNING naming what
  was dropped (Risk 3).
* **A per-tenant TTL cache** (`RLock` + `dict[key, (ts, tools)]` + `clear_mcp_cache()`),
  copying `app/tenancy/loader.py` / `app/tenancy/secrets.py`. Key includes a fingerprint
  of the resolved connection set so a rotated token or an edited server list
  invalidates immediately.

> **The cache is a correctness requirement, not just a latency one.** `MultiServerMCPClient`
> is stateless — every `get_tools()` opens fresh sessions — so without it, `reason`
> would pay a full MCP handshake on *every LLM request*, including each tool hop.
> Worse: `reason` binds the tool list and the `tools` node (Step 6) executes against it.
> If the two calls returned different lists — a server that flaked between them — the
> model would emit a call nothing can execute. One cached list per tenant per TTL makes
> bind and execute provably the same set.

Register `clear_mcp_cache()` in `tests/conftest.py::isolated_runtime` beside
`clear_secret_cache()`.

### Step 6 — the dynamic tools node (`app/brain/nodes/tools.py`, new)

The fix for `graph.py:42`. A node that resolves the tenant's full tool set at
invocation time and delegates to a freshly built `ToolNode`:

```python
async def tools(state, config):
    tenant = get_tenant_config(state["tenant_id"])
    available = native_tools_for(tenant, state.get("channel", "chat")) + await load_mcp_tools(tenant)
    return await ToolNode(available).ainvoke(state, config)
```

`graph.py` swaps `builder.add_node("tools", ToolNode(NATIVE_TOOLS))` for
`builder.add_node("tools", tools)`. Constructing a `ToolNode` is just name-indexing, so
this is cheap; `load_mcp_tools` is a cache hit within a turn. Everything else about the
graph — `tools_condition`, the `tools → reason` edge, the checkpointer, one
process-wide compiled graph — is unchanged. Delete the stale Phase 6 comment at
`graph.py:40-41`.

**`app/tools/registry.py`** — add `is_slow_tool(name) -> bool`, returning
`name in SLOW_TOOLS or name not in NATIVE_TOOLS_BY_NAME`. Every MCP tool is by
definition off the fast path (plan §11), so it must be preceded by a spoken
acknowledgement or the caller gets dead air while a third-party server thinks.
`app/brain/runner.py:124` switches to it. No new content needed —
`content/acknowledgements.json` already has a `"default"` list ("One moment.") that
`acknowledgement_for` falls back to for any unknown tool name.

`app/brain/prompts/system.py` needs **no change**: the prompt never enumerates tool
names, so MCP tools reach the model purely as bound schemas.

### Step 7 — the demo MCP server (`scripts/demo_mcp_server.py`, new)

A standalone streamable-HTTP MCP server built on the `mcp` SDK's `FastMCP`, exposing
two hotel-shaped tools that are obviously *not* tier-1 (`lookup_guest_loyalty`,
`local_events_this_week`), returning canned data. `python -m scripts.demo_mcp_server
--port 8765`.

Not imported by the app, not on any request path, and it only needs the `mcp` extra —
so it can't affect production behaviour. It's what makes the acceptance criterion
demonstrable on a laptop with no accounts, and what the live check in Step 10 drives.

### Step 8 — `scripts/register_mcp_server.py` (new) + sync

The "connect any remote MCP server" command, idempotent and re-runnable like
`provision_vapi` / `onboard_tenant`:

```
python -m scripts.register_mcp_server --tenant hotel-mzv --name tavily \
    --url 'https://mcp.tavily.com/mcp/?tavilyApiKey=${secret}' \
    --secret tvly-xxxxx [--header 'X-Foo: bar'] [--tool-allowlist search,extract]
python -m scripts.register_mcp_server --tenant hotel-mzv --list
python -m scripts.register_mcp_server --tenant hotel-mzv --name tavily --disable
python -m scripts.register_mcp_server --tenant hotel-mzv --name tavily --remove
```

Upserts the `mcp_servers` row through the Supabase **secret** key (admin path, bypasses
RLS — the same reasoning `app/tenancy/sync.py` documents: registering a server is a
backend-operator action, not a tenant reading its own data), and writes `--secret` into
Vault via the existing `set_tenant_secret` (`app/tenancy/secrets.py:134`). `--dry-run`
prints the row with the secret redacted.

`app/tenancy/sync.py` — push `tenant.mcp_servers` into the table alongside
`tenants`/`services` (`on_conflict=tenant_id,name`), so a JSON-edited server list and
the table agree and `MCP_SOURCE` can be flipped either direction without surprise.
`scripts/onboard_tenant.py:35` currently says MCP registration is "Phase 6 isn't built
yet" — that note comes out.

### Step 9 — docs

`CLAUDE.md` (Phase 6 done; new gotchas: the dynamic `tools` node and why `ToolNode`
can't be static, the cache being a correctness requirement, HTTP-only/stdio-flag, never
logging an MCP URL, `MCP_SOURCE` being the one read path that flipped early and why);
`README.md` (layout line, the tool-tier section, the stubbed/real table);
`content/README.md` (a **"Connecting any remote MCP server"** section — the two auth
shapes, `${secret}`, the name-charset rule, and the `register_mcp_server` recipe);
`.env.example`; and an amendment note on plan §11 recording the HTTP-only decision, the
`MCP_SOURCE` split and the `${secret}` design.

### Step 10 — live verification (no external accounts needed)

1. `pip install -e ".[mcp]"` in `.venv`, then `python -c "import langchain_mcp_adapters"`
   and re-run `pytest` — the Step 0 probe already told you whether to expect this to work.
2. `python -m scripts.demo_mcp_server --port 8765`
3. Add the server to `content/tenants/hotel-mzv.json`, set `MCP_ENABLED=true`,
   `MCP_SOURCE=json`.
4. `python -m scripts.chat_cli --tenant hotel-mzv --show-tools` → ask something only the
   demo server can answer. **This is the Phase 6 acceptance criterion:** the tool shows
   up, is called, and its result reaches the reply.
5. Confirm the acknowledgement fires before the MCP call (no dead air) and that
   `northside-plumbing` — no servers configured — sees none of hotel-mzv's tools.
6. Kill the demo server mid-conversation → the turn still completes, degraded, within
   `mcp_connect_timeout_seconds`. **The most important live check here**, because it's
   the failure mode a third-party server will actually produce.
7. Apply `0006_chat.sql` + `0007_mcp.sql` to the live project. Run
   `register_mcp_server`, set `MCP_SOURCE=supabase`, repeat step 4.
8. Cross-tenant: a `northside-plumbing` JWT reading `/rest/v1/mcp_servers` returns none
   of hotel-mzv's rows.
9. `GET /health` reports `"mcp": "supabase"`.
10. Latency: compare `check_availability` p50 with MCP enabled vs disabled — the cache
    should make the delta ~0. A visible delta means the cache isn't being hit.

---

## Testing

Everything offline on the existing `ScriptedChatModel` + `mock_http` + autouse
`no_network` fixtures. The adapter is never imported in tests; a fake client is injected
through `load_mcp_tools(..., client=…)`, so the suite passes whether or not the `mcp`
extra is installed — which also means CI needs no new dependency.

**Amendment (Phase 8 CI investigation, 31 Jul 2026): the last clause was wrong.**
`tests/test_mcp_loader.py` and one test in `tests/test_tenant_isolation.py` monkeypatch
`langchain_mcp_adapters.client.MultiServerMCPClient` by **string path**
(`monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)`),
not by injecting a fake `client=` into `load_mcp_tools`. A string-path `monkeypatch.setattr`
must `importlib.import_module` the target module to resolve it before patching an attribute
on it — so these specific tests **do** require the real package to be importable, even
though it's immediately replaced with a fake. `.github/workflows/ci.yml` installed only
`.[dev]`, never `.[mcp]`, so these 10 tests failed with `ModuleNotFoundError` on every CI
run since this phase merged — invisible locally on any dev box where `pip install -e
".[mcp]"` had ever been run once (as Step 0's dependency probe recommends), silently
masking the gap for months. Fixed by adding `mcp` to CI's install line; Linux has none of
the compiled-dependency risk (`rpds-py`, `cryptography`, `pywin32`) this phase's Risk 1
weighed against installing it unconditionally — that risk is specifically a Windows
dev-box concern.

New:

* `tests/test_mcp_registry.py` — JSON vs Supabase dispatch; the Supabase request carries
  `tenant_id=eq.` and the tenant JWT; `enabled=false` rows excluded; **a registry error
  returns `[]` and logs, never raises**.
* `tests/test_mcp_connections.py` — `"http"` → `"streamable_http"`; stdio refused unless
  the flag is set; `${secret}` substituted into url *and* headers; the bearer default;
  a `TenantSecretError` drops one server and keeps the rest; **`redacted()` hides a
  query-string key**; an illegal server name fails at config load.
* `tests/test_mcp_loader.py` — cache hit within a turn and across turns; TTL expiry;
  fingerprint change invalidates; one slow server times out without blocking the others;
  `tool_allowlist` and `mcp_max_tools` truncation; **`ImportError` degrades to `[]` with
  a warning**; `MCP_ENABLED=false` short-circuits before any I/O.
* `tests/test_mcp_tools_node.py` — **the regression that motivates Step 6**: a fake MCP
  tool bound in `reason` is actually executed by the `tools` node; native tools still
  execute unchanged; a tenant with no servers behaves byte-identically to today.
* `tests/test_tenant_isolation.py` — extend: tenant A's tool set never contains a tool
  from tenant B's server.
* `tests/test_migrations.py` picks up `0007_mcp.sql` automatically.

## Risks

1. **The dependency chain is the schedule risk, not the code.** `mcp` 1.28.1 pulls
   `jsonschema`→**`rpds-py`** (compiled Rust), `pyjwt[crypto]`→**`cryptography`**
   (compiled — the dependency Phase 4 explicitly designed around, see its Risk 1), and
   **`pywin32`**. CLAUDE.md's first environment gotcha is a blocked compiled DLL taking
   down *every import in the project*. Mitigated three ways: the throwaway-venv probe in
   Step 0, the optional extra, and the lazy import that degrades to `[]`. Linux deploy
   is unaffected — this is a dev-box risk only, exactly like `psycopg`.
2. **Version pin.** `>=0.1` would resolve to a release requiring `langchain-core<1.0`
   and silently downgrade the stack, the failure CLAUDE.md already records for the
   `google` extra. Fixed by pinning `>=0.3`.
3. **Every bound tool schema is re-sent on every request, forever.** README's cost
   section puts the fixed floor at ~1,460 tokens (~790 of it tool schemas for five
   tools); MCP tools add to that on every turn of every conversation, including turns
   that never need them, and one request per tool hop. `mcp_max_tools` caps it;
   `tool_allowlist` is the per-tenant scalpel. Worth re-reading `test_llm_cost.py`'s
   framing before turning MCP on for a tenant on a free tier.
4. **Tool-name legality is a whole-request failure.** A prefixed name outside
   `^[a-zA-Z0-9_-]{1,64}$` makes the provider 400 the entire request — every turn, not
   just the MCP one. Closed by the `name` validator at config load.
5. **A third-party MCP server's tool descriptions enter the model's context verbatim.**
   That is untrusted text with instruction-shaped authority — the classic MCP prompt
   injection surface. We can't sanitise it away; `tool_allowlist` plus the per-tenant
   allowlist (never a global server list) limits blast radius, and native tools staying
   native means booking/escalation can't be reached this way. State it plainly in
   `content/README.md` rather than pretending it's solved.
6. **`stdio` is remote code execution** if tenant config ever becomes self-serve.
   Off by default; the flag is an operator decision.
7. **URL-as-credential.** Query-string auth means a logged URL is a leaked key. Closed
   by `redacted()` and by never logging a raw connection — the `vapi_schema.redacted()`
   precedent.
8. **A dead MCP server on a live phone call.** Bounded by `asyncio.wait_for` and
   per-server loading; verified by live check 6, which is the one worth doing twice.
9. **More tools makes Groq's inline-tool-call leakage more likely** (CLAUDE.md's first
   gotcha). `app/brain/sanitize.py` and the `reason.py` retry already handle it — but
   watch for it during the live run, since tool count is exactly the variable that
   provokes it.

## Deferred

A concrete search/scraper server (needs a key — `plans/phase10.md` item 11), Google
Sheets and Supabase's own hosted MCP server (§11's other examples — same config path,
no new code), MCP tool-result caching, per-tenant MCP usage metering (waits on the same
usage-tier model as Phase 5's rate limiting), and an admin UI for the registry (Phase 8
territory). `TENANT_SOURCE=supabase` for tenant *config* stays deferred — `MCP_SOURCE`
flipping early does not flip it, for the reasons in "Why the table, and why only here".

## Est. effort

2–3 days, matching plan §15. Steps 1–6 are a day and mechanical. Step 6 is small in
code and the highest-value part of the phase. The genuine unknowns are the dependency
probe (Step 0) and the adapter's prefix separator (Step 5) — both cheap to settle, both
worth settling before writing anything downstream of them.
