# Phase 9.1 — Draft/Deploy, link & handoff buttons, channel flags

> **Step 0 of implementation:** copy this file to `D:\Projects\My\AI-Reception\plans\phase9.1.md`
> (plan mode can only write to the scratch plan path). The voice tester is planned here as
> **Phase 9.2** and gets its own `plans/phase9.2.md` at that time.

---

## Context

Three requests, all reaching into the same admin/config write path that Phase 8 built.

**1. There is no draft state.** Today `PUT /admin/api/tenants/{id}` → `save_tenant()` →
`sync_tenant()` → `refresh_tenant_repository()`. Every keystroke you save is live on the
next turn. That immediacy was deliberate — Phase 8's "phantom edit" fix exists precisely
because an admin write that *didn't* reach the running bot was a silent, unloggable bug.
So a draft/live split has to be built as the *opposite* of the phantom edit, not a
reintroduction of it: the gap between "saved" and "live" must be **loud and visible in the
UI**, and `refresh_tenant_repository()` must fire on Deploy and only on Deploy.

**2. There is no hosted per-tenant test surface.** `GET /widget/demo` serves
`widget/demo.html` with `data-widget-key="pk_widget_hotelmzv_demo"` hardcoded in the markup
— one tenant, no parameterisation. Testing any other bot means hand-building an HTML file.

**3. The widget can render exactly two kinds of affordance**, both accidental byproducts of
tools that exist for other reasons: quick-reply chips from `check_availability`'s
`kind: "slots"` artifact, and a `tel:` link from `escalate`'s `kind: "handoff"` artifact.
There is no way for the model to say "here's a button that goes somewhere."

**Outcome:** every bot — current and future, with no per-bot setup — gets a draft→Deploy
workflow with switchable deploy history, a signed shareable Test Agent link, and the ability
to offer link/handoff buttons the model chooses at runtime from a tenant-declared catalog.

### Decisions taken (from clarification)

| | |
|---|---|
| Scope | 9.1 = features 1 + 2 + channel flags. Voice tester = **9.2**, built on 9.1's signed-link and channel-flag seams. |
| Versioning | A **new immutable version row per *deploy***, not per save. Saves overwrite one mutable draft. Admin can switch live to any past version, and delete any non-live version. |
| Link source | **Tenant link catalog** in config. The model picks *slugs*; the server resolves slug → URL. The model never emits a URL. Catalog entries are rendered into the system prompt so the model knows what exists for that bot. |
| Test link | **Signed public URL**, works logged out, expires. Carries a `mode` claim so 9.2's voice tester reuses the same link and auth path. |
| Channels | `channels: {chat: {enabled}, voice: {enabled}}`, defaults true/true. |

---

## Feature 1 — Draft vs live, Deploy, version history

### Data model — `app/db/migrations/0012_versions.sql`

```sql
alter table public.tenants
  add column if not exists draft_config     jsonb,          -- null = no unpublished edits
  add column if not exists draft_updated_at timestamptz;

create table if not exists public.tenant_versions (
  id             text primary key,
  tenant_id      text not null references public.tenants(tenant_id) on delete cascade,
  version_number integer not null,
  config         jsonb   not null,
  note           text    not null default '',
  deployed_by    text    not null default '',
  deployed_at    timestamptz not null default now(),
  is_live        boolean not null default false,
  unique (tenant_id, version_number)
);
create unique index tenant_versions_one_live_idx
  on public.tenant_versions (tenant_id) where is_live;
```

Three deliberate shapes:

- **`draft_config` is a whole-`TenantConfig` JSONB blob**, not a mirror of the relational
  columns. `sync_tenant()` fans a live config out across `tenants` + `services` +
  `mcp_servers`; a draft must never touch those tables, because those tables *are* what the
  runtime reads. One inert column is the only way to guarantee a draft can't leak live.
- **`is_live` + a partial unique index, not a `tenants.live_version_id` FK.** A pointer
  column would make `tenants` and `tenant_versions` mutually referential, which fights both
  `purge_tenant`'s FK-ordered deletes and "delete an old version". The partial index gives
  the same one-live-per-tenant guarantee with no cycle.
- **No `delete` grant**, matching `0010_lifecycle.sql`'s reasoning: version deletion runs on
  the Supabase secret key (`service_role`), which already holds DELETE via the project's own
  `ALTER DEFAULT PRIVILEGES`. `app_backend` gets `select, insert, update` + a
  `tenant_isolation` policy + `enable`/`force` RLS — required anyway by
  `tests/test_migrations.py`'s lint.

Also in this migration: nothing for `updated_at` (Phase 8's version token stays as-is for
the *live* row; the draft gets its own `draft_updated_at`).

### Write path — `app/tenancy/admin.py`

Existing `save_tenant()` stays as the **live-write primitive** and keeps its version check,
voice-consent gate and `refresh_tenant_repository()`. New functions wrap it:

```python
async def get_draft(tenant_id, *, client=None) -> tuple[TenantConfig | None, str | None]
async def save_draft(config, *, expected_version, client=None) -> str      # -> new draft_updated_at
async def discard_draft(tenant_id, *, client=None) -> None
async def deploy_tenant(tenant_id, *, note="", deployed_by="", client=None) -> TenantVersion
async def list_versions(tenant_id, *, limit=50, client=None) -> list[TenantVersion]
async def switch_to_version(tenant_id, version_id, *, client=None) -> TenantVersion
async def delete_version(tenant_id, version_id, *, client=None) -> None
```

`deploy_tenant` is the only one that reaches live, in this order:

1. Read `draft_config`; **409** if null (nothing to deploy).
2. `TenantConfig.model_validate(draft)` → **422** with `_validation_errors()` mapping.
   Re-validating at deploy, not trusting the save-time validation, is what makes a
   schema change between save and deploy a clean 422 rather than a broken live bot.
3. `save_tenant(config, expected_version=None)` — reuses the existing consent gate,
   `sync_tenant()` fan-out (including its service/MCP **deletes**), and repository refresh.
4. Insert the `tenant_versions` row: `version_number = max+1`, flip `is_live` off the old
   row and on the new one.
5. Null out `draft_config` / `draft_updated_at`.

Step 3 before step 4 is intentional: a failed live write must not leave a version row
claiming to be live.

`switch_to_version` is the rollback path and does **2 → 3 → flip `is_live`** with no new
row and no version number burned. The 422 in step 2 is load-bearing here — a version
serialised under an older `TenantConfig` schema may no longer validate, and that must
surface as "these fields are no longer valid," never a 500.

`delete_version` **409s on the live version** (and the partial index + `on delete cascade`
make the FK side safe regardless).

Two existing things must be updated, not left alone:
- `_PURGE_TABLES` gains `"tenant_versions"` immediately before `"tenants"`. Same class of
  gap Part C already fixed once — the FK cascades, but the per-table counts `purge_tenant`
  logs for audit would under-report.
- `set_tenant_status` (archive/restore) stays **outside** the draft flow. It is a status
  flip, not a config edit, and gating it behind Deploy would mean you can't archive a bot
  without publishing unrelated draft edits.

### Routes — `app/channels/admin.py`

| Method | Path | Notes |
|---|---|---|
| GET | `/tenants/{id}` | **Changed.** Now returns `{config, live_config, has_draft, _version, _draft_version, live_version: {...} \| null, ...}`. `config` = draft when one exists, else live — so the editor works unchanged. |
| PUT | `/tenants/{id}` | **Changed.** Same merge + validate + operator-only checks, but writes the **draft**. `If-Match` now targets `draft_updated_at`. Does **not** refresh the repository. |
| POST | `/tenants/{id}/deploy` | Body `{note?: str}`. → `{version, deployed_at, ...}` |
| POST | `/tenants/{id}/draft/discard` | 204-ish; clears the draft |
| GET | `/tenants/{id}/versions` | list, newest first, `is_live` flagged |
| POST | `/tenants/{id}/versions/{vid}/switch` | rollback / roll-forward |
| POST | `/tenants/{id}/versions/{vid}/delete` | 409 if live (POST, not DELETE — CORS `allow_methods` is GET/POST/OPTIONS; PUT already works only because the SPA is same-origin, don't widen that surface further) |

Deploy/switch/delete are **operator-only** (same manual `principal.kind` check
`create_tenant_route` and `purge_tenant_route` already use) — `AdminPrincipal.may_write`
covers per-tenant access, but publishing is an operator action until `plans/phase10.md`
item 14's tenant-login branch exists.

`create_tenant` deploys immediately (writes live + version 1) — a bot that exists only as a
draft would be invisible to every read path and confusing.

**Backfill:** tenants that predate this have no versions. `deploy_tenant` handles "no live
version yet" by creating version 1. No migration backfill — a version row should mean
"this was deployed through this system," not a synthesised fiction.

### Admin UI — `admin/src/`

- `views/Config.tsx`: the single **Save changes** button becomes **Save draft**, plus a
  persistent banner when `has_draft` — *"Draft — not live. N changes pending."* — carrying
  **Deploy** and **Discard draft**. Deploy opens a small confirm with an optional note and
  a plain field-level diff of draft vs `live_config` (a flat key walk; no diff library).
  The banner is the entire mitigation for reintroducing a save/live gap — it must be
  impossible to miss.
- New `views/Versions.tsx` + a `"versions"` `Tab` in `router.ts`/`TenantView.tsx`: the
  deploy timeline, each row with **Make live** and **Delete**.
- `api.ts`: `saveTenantDraft`, `deployTenant`, `discardDraft`, `listVersions`,
  `switchToVersion`, `deleteVersion`; `TenantConfig` gains `_draft_version`, `has_draft`,
  `live_config`.
- Deploy is where the "more sub-options later" room lives: the route already takes a body,
  the version row already carries `note`/`deployed_by`, and the button is a menu-shaped
  control from day one.

---

## Feature 1b — Test Agent link (shared with 9.2)

### Signing — `app/channels/test_links.py` (new)

A near-copy of `app/channels/widget_auth.py`'s stdlib HMAC pattern (no PyJWT), separate
secret and separate claim set so a leaked test link can never be replayed as a chat session:

```python
@dataclass(frozen=True, slots=True)
class TestLinkClaims:
    tenant_id: str
    mode: Literal["chat", "voice"]      # "voice" is minted-and-rejected until 9.2
    variant: Literal["live"]            # room for "draft" later; only "live" now

def mint_test_token(tenant_id, *, mode="chat", ttl_seconds=None) -> str
def verify_test_token(token) -> TestLinkClaims | None    # never raises, fails closed
```

Secret: new `TEST_LINK_SECRET` setting, falling back to `widget_session_secret` then the
per-process fallback — same three-tier degradation `widget_auth` already uses, so dev needs
no new env var. TTL: new `test_link_ttl_seconds`, default 24h.

### Routes

- `POST /admin/api/tenants/{id}/test-link` — body `{mode?}`, returns
  `{url, expires_at}`. `url` is built from `settings.public_base_url`; **422 with a clear
  message when that's unset**, rather than emitting a broken `None/test/...`.
- `GET /test/{token}` — serves a full-page HTML "host site" with the **real widget** embedded
  and auto-opened. Registered *before* nothing in particular (it's not under `/admin`, so
  the `/admin/{path:path}` catch-all ordering trap doesn't apply), but the ordering test in
  `tests/test_api.py` should be extended to pin that.
- `POST /test/session` — takes the test token, returns the **same `ChatSessionResponse`
  shape** `/chat/session` returns (server-minted `session_id` + widget session token).

`/test/session` exists rather than reusing `/chat/session` for three reasons: it needs no
widget key at all (so a tenant with an empty `widget_keys[]` is still testable), it bypasses
`chat.allowed_origins` legitimately (the page is served from the app's own origin), and it
keeps the test path off the public widget rate-limit buckets. It gets its own limiter
bucket via the existing `ratelimit._hit()` helper.

### Widget change — additive only

`widget/src/main.ts` gains one optional data attribute, `data-test-token`; `api.ts::startSession`
posts to `/test/session` with it instead of `/chat/session` with a widget key. Existing
`<script src=… data-widget-key=…>` tags are untouched — the frozen contract in
`widget/README.md` is *added to*, never changed. Reusing the real widget rather than
building a second chat UI is the point: the test page then exercises the actual embed path,
including the new action buttons, and can't drift from what a client sees.

The Test Agent button sits in the tenant header in `TenantView.tsx` so it's on every tab,
and is **greyed per channel flag** — which is how 9.2's voice mode appears with no further
UI work.

---

## Feature 2 — Link & handoff buttons

### Config — `app/tenancy/models.py`

```python
class TenantLink(BaseModel):            # frozen, like every model here
    slug: str                           # ^[a-z0-9][a-z0-9-]{0,47}$, mirroring McpServerConfig
    label: str                          # button text
    url: str | None = None              # required for type="link", must be http(s)
    description: str = ""               # what the model reads to decide
    type: Literal["link", "handoff"] = "link"
```

`TenantConfig` gains `links: list[TenantLink] = []`, with a `_unique_link_slugs` model
validator matching the existing `_unique_service_slugs`. It round-trips inside the `config`
JSONB automatically (it's outside `_TENANT_COLUMNS`), so `sync.py` and
`supabase_repository.py` need no changes.

`type: "handoff"` needs no URL — it renders a button that sends a canned phrase, which the
model then answers by calling the existing `escalate`. That reuses the entire escalation
path (escalator selection, `Escalation` row, alert SMS, the existing `handoff` artifact)
instead of building a second one.

### Tool — `app/tools/action_tools.py` (new)

```python
@tool(response_format="content_and_artifact")
async def offer_actions(slugs: list[str], config: RunnableConfig = None) -> tuple[str, dict]
```

- Reads the tenant via `tenant_from_config(config)` — never a model-supplied tenant id.
- Resolves each slug against `tenant.links`; unknown slugs are dropped with a WARNING, not
  an error (a partial answer beats a failed turn).
- Returns `(text, {"kind": "actions", "actions": [{"type", "label", "url", "slug"}]})`.
  Empty resolution returns `(msg, {})` — the empty-dict-means-no-artifact convention
  `check_availability`'s error branches already established.
- Docstring is what the model actually sees as the schema description; it says *offer these
  as clickable buttons, don't paste the URL into your reply text.*

### Registry — `app/tools/registry.py`

```python
if tenant.links and channel == "chat":
    tools.append(offer_actions)
```

This is the **first time `native_tools_for`'s `channel` parameter is used** — it has been
`del channel` since Phase 1, with a docstring promising exactly this. A voice caller can't
click a button, so binding it there would only invite the model to read URLs aloud.
`NATIVE_TOOLS` stays fixed at five so
`test_native_tools.py::test_critical_path_tools_are_all_native` keeps its meaning;
`offer_actions` joins `search_knowledge` as a *conditional* native tool. Add it to
`SLOW_TOOLS`? **No** — it's a pure in-memory config lookup, the only native tool that is.

Correctness of bind-vs-execute rests on the same guarantee as `search_knowledge`: `reason`
and the dynamic `tools` node both call this one function. The test that proves it must be a
**full graph turn** that binds *and* executes, not a static list comparison.

### Event pipeline

- `app/brain/runner.py`: `EventType` gains `"actions"`; a `_actions_artifact(message)`
  reader alongside `_suggestions_artifact` / `_handoff_artifact`, dispatched on
  `kind == "actions"` in the same `tools`-node update block.
- `app/channels/chat.py`: `"actions"` joins `_PUBLIC_EVENT_TYPES`.
- `app/channels/vapi_llm.py`: no change needed — `_sse_chunks` only acts on
  `is_spoken` / `error` / `handoff` and debug-logs everything else. Confirm with a test
  rather than assuming.

### Prompt — `content/system-prompt.md`

New `${links}` placeholder (rendered by `render_system_prompt` in
`app/brain/prompts/system.py`, exactly like `${knowledge_rule}`: empty string when
`tenant.links` is empty), listing `slug / label / description` per entry, plus one bullet
in **How you work**: *"When a link or a human would help, call offer_actions with the
matching slugs — never paste a URL into your reply."*

Known caveat to document, not solve: a tenant with `system_prompt_override` set won't get
`${links}` unless their override includes it — the same caveat `${knowledge_rule}` already
carries.

### Widget — `widget/src/`

- New `ActionButtons.tsx`, sibling to `QuickReplies.tsx`.
- `useStream.ts`: `"actions"` joins `BrainEventType`.
- `App.tsx`: `applyEvent` stores `actions` on the message (same shape as `suggestions`
  today); `type: "link"` renders `<a target="_blank" rel="noopener noreferrer">`,
  `type: "handoff"` renders a button whose click goes through the existing `send(value)`
  path — literally `QuickReplies`' `onPick` behaviour, so no new plumbing.
- Style in `styles.css` next to `.ai-recept-chip` / `.ai-recept-callto`.
- **`npm --prefix widget run build` and commit `dist/` + `.buildhash`** or
  `tests/test_widget_bundle.py` fails. Same for `admin/`.
- Admin `Config.tsx` gains a `LinksSection`, modelled on the existing `ServicesSection`.

---

## Feature 3 (channel flags, shipping in 9.1)

`TenantConfig` gains `channels: ChannelSettings` where
`ChannelSettings(chat: ChannelToggle = enabled, voice: ChannelToggle = enabled)` and
`ChannelToggle(enabled: bool = True)` — a nested object rather than two booleans
specifically so 9.2 can hang `voice.stt_provider` etc. off it without another schema change.

Enforcement lives in the adapters, not `resolve_tenant_id` (which has no channel argument):
a `require_channel_enabled(tenant, channel)` helper in `app/tenancy/loader.py` raising a
`ChannelDisabledError(TenantNotFoundError)` — subclassing so every existing
`except TenantNotFoundError` handler already covers it, exactly the trick
`TenantArchivedError` used in Phase 9 Part B. Called from `chat.py`'s two endpoints,
`vapi_llm.py`, and `/test/session`.

Defaults are true/true, so no tenant behaviour changes and no test churn.

---

## Files

**New:** `app/db/migrations/0012_versions.sql`, `app/channels/test_links.py`,
`app/tools/action_tools.py`, `admin/src/views/Versions.tsx`,
`widget/src/ActionButtons.tsx`, `tests/test_tenant_versions.py`,
`tests/test_admin_deploy.py`, `tests/test_test_links.py`,
`tests/test_action_tools.py`, `tests/test_channel_flags.py`.

**Modified (core):** `app/tenancy/admin.py` (draft/deploy/version functions, `_PURGE_TABLES`),
`app/channels/admin.py` (7 routes), `app/tenancy/models.py` (`TenantLink`,
`ChannelSettings`, validators), `app/tools/registry.py`, `app/brain/runner.py`,
`app/channels/chat.py`, `app/tenancy/loader.py`, `app/brain/prompts/system.py`,
`app/config.py`, `app/main.py` (two `/test/...` routes), `content/system-prompt.md`.

**Modified (frontend):** `admin/src/{api.ts, router.ts, views/TenantView.tsx,
views/Config.tsx}`; `widget/src/{main.ts, api.ts, useStream.ts, App.tsx, styles.css}`;
both `dist/` bundles + `.buildhash` rebuilt and committed. `widget/README.md` gains the
new optional attribute; `content/README.md` gains the links catalog.

**Unchanged on purpose:** `app/tenancy/sync.py`, `app/tenancy/supabase_repository.py`,
`app/brain/graph.py`, `app/brain/nodes/*`. The draft never touches the runtime read path —
that is the whole safety argument, and a diff touching those files means the design slipped.

---

## Verification

**Offline (`pytest`, `ruff check .`):**
- Draft round-trip: PUT → live config unchanged → `refresh_tenant_repository` **not** called
  → Deploy → live changed → repository refreshed. The negative half is the phantom-edit guard.
- Stale `If-Match` on a draft 409s; an invalid draft 422s with per-field `loc` paths.
- Deploy with no draft 409s. Two deploys → version 1, 2, exactly one `is_live`.
- `switch_to_version` moves live without burning a version number; deleting the live
  version 409s; deleting a non-live one succeeds.
- A stored version that no longer validates (simulate by injecting a bad config row) 422s.
- `_PURGE_TABLES` order test (`test_admin_tenant_crud.py::test_deletes_in_fk_order`) updated.
- `test_migrations.py`'s RLS lint passes for `tenant_versions`.
- Test links: mint→verify round-trip, expiry, tampering, cross-tenant replay, and a widget
  session token rejected by `verify_test_token` (and vice versa).
- `offer_actions`: real graph turn that binds *and* executes it; unknown slug dropped;
  voice channel never binds it; `"actions"` reaches a chat SSE stream and never reaches
  `/chat/completions`; tenant A can't resolve tenant B's slugs.
- Channel flags: `chat.enabled=false` → `/chat/session` 404s; `voice.enabled=false` →
  `/chat/completions` 404s; defaults change nothing.
- Both `test_widget_bundle.py` and `test_admin_bundle.py` green (i.e. bundles rebuilt).

**Live (against the real Supabase project + Railway — the class of bug offline tests
structurally cannot catch, per Phase 8's `_admin_client` lesson):**
1. Apply `0012_versions.sql` via the same one-off `psycopg`/`DATABASE_URL` script used for
   `0001`–`0011`; confirm columns, table, partial unique index, RLS enabled **and** forced,
   and that `anon`/`authenticated` hold nothing on `tenant_versions`.
2. `/admin` → edit `hotel-mzv`'s greeting → Save draft → **hit the live bot and confirm it
   still says the old greeting** → Deploy → confirm the new one on the very next turn, no
   restart. This is the acceptance criterion; both halves are required.
3. Versions tab shows v1 → v2; **Make live** on v1 → live bot reverts on the next turn.
4. Test Agent → new tab → real chat with real Cal.com slots as quick-reply chips, from a
   logged-out browser; confirm an expired/tampered token is refused.
5. Add two `links` to `hotel-mzv`, deploy, then ask the bot something that should surface
   one — confirm the model calls `offer_actions`, the button renders and opens, and the
   handoff button drives a real `escalate`.
6. Set `channels.chat.enabled=false` on a scratch tenant, deploy, confirm `/chat/session`
   404s, restore.
7. Confirm the deployment switch on Railway: `ADMIN_ENABLED` / `ADMIN_AUTH_TOKEN` /
   `TENANT_SOURCE=supabase` must be set **together** (`app/preflight.py:89`) — still unset
   today per CLAUDE.md, and Deploy is meaningless without `TENANT_SOURCE=supabase`.

**Deliberately not claimed done without eyes on it:** the admin and widget UI changes get a
real browser click-through. Part B and Part C both shipped UI verified only by build+route
tests; that gap doesn't repeat here.

---

## Voice-tester preview — planned, not built here

> **Renumbered:** this was written as "Phase 9.2". The 9.2 slot was later reassigned to
> deterministic flows / rich buttons / generic-template cards (`plans/phase9.2.md`), so the
> voice tester below is now **Phase 9.3**. Nothing about its design changed — every "9.2" in
> this file refers to it, and every seam it depends on (the `mode` claim, `ChannelToggle`,
> the greyed-per-channel-flag Test Agent menu) shipped in 9.1 exactly as described.

Confirmed viable on Railway with **no new platform, no Dockerfile change, no new system
deps**, because the server is a byte relay, not a media processor:

```
browser mic (AudioWorklet, PCM16 16k)
   ─ws─▶ FastAPI /voice/live ─ws─▶ Deepgram streaming STT
                              ─────▶ stream_turn(channel="voice")   [unchanged]
                              ─ws─▶ Cartesia streaming TTS
   ◀ws─ PCM16 24k audio frames
```

- **WebSocket, not WebRTC** — Railway's proxy is HTTP/TCP with no UDP ingress or TURN;
  WebRTC would force a media server. Browser-side `echoCancellation: true` handles the
  bot-hears-itself problem without one.
- **Hosted STT/TTS, not self-hosted** — no GPU on Railway. `deepgram_api_key` already
  exists in `app/config.py` and is read by nothing; this is the slot it fills.
- **Raw PCM end to end** — no ffmpeg, no transcode, no apt layer.
- Provider seams from day one: `app/voice/stt/base.py` + `deepgram.py`,
  `app/voice/tts/base.py` + `cartesia.py`, selected from `VoiceSettings`. Cartesia voice
  cloning already exists (`app/tenancy/voice.py`) and plugs straight in. *"Omni voice
  studio"* — relayed as given by the user, unverified as a product name; treat as a
  candidate future provider to confirm, not an assumption.
- Reused unchanged: `stream_turn`, `sanitize.py` (`RepeatSuppressor`, inline-tool-call
  filter), `acknowledge.py`, the `is_spoken` filtering pattern, `FIRST_TOKEN_BUDGET_MS`
  instrumentation. Vapi-specific and *not* reused: `vapi_schema.py`, `webhooks.py`,
  `require_vapi_secret`, transcript reseeding.
- Reaches the user via the **same signed link** — `/test/{token}` with `mode: "voice"` — and
  is gated by the same `channels.voice.enabled` flag, both of which 9.1 builds.
- Latency budget (§13, 600–800ms) is the design constraint; region alignment is already
  right (Railway `us-east4` / Supabase `us-east-1` / Deepgram / Cartesia / Gemini all US),
  and this path drops a hop versus Vapi.
- Known constraint to respect: all relay runs on the one shared event loop. Any CPU-bound
  work there stutters live audio — the discipline `app/rag/ingest.py` already applies with
  `asyncio.to_thread` has to hold.
