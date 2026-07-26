# Phase 5 — Chatbot channel

## Context

Phases 1–4 are done. `POST /chat` (SSE) already exists and already drives the same
graph as voice — `tests/test_api.py` proves a job can be booked through it. What
does *not* exist is anything a business could put on its website: `widget/`
contains a README and nothing else.

Plan §15's acceptance criterion is *"the web widget books a job identically to the
phone."* Half of that is already true. Phase 5 is the other half — and a set of
things about `/chat` that are harmless while only `curl` and `chat_cli` can reach
it, and become live defects the moment a browser can:

1. **No CORS anywhere in the app** (`app/main.py` has zero middleware) — a
   cross-origin widget cannot call `/chat` at all.
2. **`session_id` defaults to `"web"`** (`app/channels/chat.py:25`), so every
   anonymous visitor of a tenant shares checkpointer thread `hotel-mzv:web` and
   reads each other's conversation.
3. **`tenant_id` is trusted from the request body** (`chat.py:26` →
   `loader.py:81-83`), letting any caller pick any tenant. `app/channels/vapi_llm.py:16-17`
   states "never trust the body for tenancy" as an invariant, and
   `widget/README.md` already claims chat behaves this way. It doesn't.
4. **`tool_start` / `tool_result` are forwarded verbatim to the client**
   (`chat.py:48-55`), including raw tool args and output. The voice channel
   deliberately filters these (`vapi_llm.py:141-143`); chat does not.
5. **A chat conversation is recorded nowhere.** A voice call writes a `calls` row
   (`app/channels/webhooks.py:55`); chat writes nothing to any store. Transcripts
   exist only as checkpointer state, which the Phase 4 `pg_cron` job deletes after 48h.
6. **An unknown `widget_key` raises inside the generator**, after `200 OK` and
   headers are already sent — the browser sees a truncated stream, never an error.

The outcome: a paste-one-script-tag widget that books real jobs, a `/chat` that is
safe to expose to a browser, and durable chat transcripts at parity with voice.

## Decisions locked

| | |
|---|---|
| **Scope** | **Web widget only.** WhatsApp stays in `plans/phase10.md` item 4 — it needs a Twilio WhatsApp sender and Twilio is parked by client decision. |
| **Widget auth** | **Session handshake + signed token.** `POST /chat/session` takes the public widget key and returns a server-minted `session_id` + short-lived HMAC token. Solves per-visitor threads, body-trusted tenancy, and "a browser cannot hold `API_AUTH_TOKEN`" in one move. |
| **Widget build** | **Bundled — Preact + TypeScript + Vite**, library mode, single IIFE, Preact inlined (~4KB). Chosen for the rich UI below. |
| **Widget UI** | A **normal chat** (launcher, streaming replies, typing indicator, history, mobile, Shadow DOM) **plus quick replies** — the feature the client specifically wants. |
| **Transcripts** | **New `chat_sessions` + `chat_messages` tables**, RLS + grants + a `pg_cron` retention window, mirroring `calls`. |
| **Rate limiting** | **Out of scope — deferred to `plans/phase10.md`.** A future subscription tier (or bring-your-own-LLM) decides who gets how much usage; a limiter built now would be built against the wrong model. |

### The embed contract is the thing to freeze

Once a client pastes `<script src="…" data-widget-key="pk_…">` into their site, that
contract can never change — you cannot make them re-paste it. Everything behind it
(Preact, Vite, the whole bundle) is replaceable at will. So the script tag, the
`data-` attributes, and the `/chat/session` → `/chat` protocol get designed once and
treated as frozen; the implementation does not.

### How quick replies work without putting channel logic in the graph

Two sources, neither of which asks the LLM to emit UI markup (that would be parsed
out of prose by `app/brain/sanitize.py`, which exists precisely because Groq already
leaks structured payloads into text):

* **At handshake** — `/chat/session` returns the tenant's `greeting` and service
  list straight from `TenantConfig`. The widget renders opening chips ("Book a
  room", "Reserve a table"). Zero graph involvement.
* **Mid-conversation** — `check_availability` gains
  `response_format="content_and_artifact"` and returns
  `(text, {"kind": "slots", "service": …, "slots": [{"start_iso", "label"}]})`.
  The runner reads the artifact and emits a new **`suggestions`** `BrainEvent`.

This is exactly the mechanism `escalate` already uses (`app/tools/messaging_tools.py:85,154-161`
→ `app/brain/runner.py:231-244`), and CLAUDE.md records it as verified on
langchain-core 1.5.0: *"a bare `.ainvoke({...})` still gets a plain string back"*, so
every existing `check_availability` test passes untouched. `suggestions` is **not**
`is_spoken`, so it can never become audio.

A clicked chip sends its **human label** ("8:30pm") as an ordinary user message —
never the raw ISO. The model already holds the numbered list with `slot_start_iso`
values in its own context, so `book_job` still gets a verbatim ISO. The widget stays
a renderer; the graph never learns what a chip is.

## What I need from you

1. **Node.js 20+ on this box** (`node --version`). It's the one new prerequisite —
   the Python side is unaffected, and `pytest` / `ruff` stay the only CI tools.
2. **Nothing else.** No new accounts, no new credentials. Supabase, Cal.com and the
   tenants are already live.

Optional, for a genuinely cross-origin manual test: any static HTML file served
from a different origin (a second `python -m http.server` on another port is enough
— a real domain is a Phase 7 concern).

---

## Implementation

Each step ends with a green `pytest`. Steps 1–6 are Python and need no Node.

### Step 0 — baseline
`pytest` → record the count. `node --version` → confirm the toolchain.

### Step 1 — config, tenant model, hygiene (no behaviour change)

`app/config.py` — new **real `Settings` fields** (never ad-hoc `os.environ`;
`hermetic_settings` strips only names matching a field, the lesson recorded in
`plans/phase3.md`):

* `widget_session_secret: str | None` — HMAC key for session tokens. When unset,
  `app/channels/widget_auth.py` generates a random per-process key at import, so
  dev works with no config and tokens simply don't survive a restart. Fails safe,
  matching the fail-open-when-unconfigured convention in `app/channels/security.py:30-31`.
* `widget_session_ttl_seconds: int = 3600`
* `chat_transcript_retention_days: int = 30` (mirrors the `calls.transcript` window)

`app/tenancy/models.py` — one new frozen sub-model, `ChatSettings`, on `TenantConfig`:

```python
class ChatSettings(BaseModel):     # frozen, like every sibling
    allowed_origins: list[str] = []    # empty = any origin (dev default, documented)
    accent_color: str = "#0f766e"
    launcher_label: str = "Chat with us"
    quick_replies: bool = True
    greeting: str | None = None        # falls back to TenantConfig.greeting
```

`widget_keys: list[str]` (`models.py:189`) already exists and is already synced to
Supabase (`app/tenancy/sync.py:37,67`) — no change needed there.

Add `.env.example` entries with comments. Add `"widget"` to `GET /health` reporting
whether `widget/dist/widget.js` is present, so a deploy that forgot the bundle is
one `curl` away from visible — the same reasoning as Phase 4's `store` field.

### Step 2 — session tokens (`app/channels/widget_auth.py`, new)

Stdlib `hmac`/`hashlib`/`base64` only, copying the shape of `app/db/auth.py` — that
file already proves the pattern in this codebase and is why PyJWT is not a dependency.

* `mint_session_token(tenant_id, session_id) -> str` — payload `{tid, sid, exp}`,
  signed HS256, urlsafe-b64.
* `verify_session_token(token) -> SessionClaims | None` — constant-time compare via
  `hmac.compare_digest`, expiry checked. Returns `None` on anything malformed; never
  raises.
* `new_session_id() -> str` — `web_<hex12>`, mirroring the id style in
  `app/db/models.py:17`.

Unlike `app/db/auth.py` we **do** verify here (that file is sign-only), so this is
the one place with verification logic — keep it small and test it hard.

### Step 3 — the handshake and a rewritten `/chat` (`app/channels/chat.py`)

**`POST /chat/session`** — public, no bearer. Body `{widget_key}`, plus the `Origin`
header.

1. Resolve the tenant with the existing `resolve_tenant_id(widget_key=...)`
   (`app/tenancy/loader.py:94-97`, backed by `find_by_widget_key` at
   `repository.py:68-73`). Unknown key → **404 before any stream starts** (closing
   defect 6).
2. Check `Origin` against `tenant.chat.allowed_origins` when non-empty → 403.
3. Mint `session_id` + token.
4. Return `{session_id, token, expires_in, tenant: {name, greeting, accent_color,
   launcher_label, services: [{slug, name, duration_minutes, price_usd}]}}`.

That services list is what the widget turns into opening chips — no extra endpoint.

**`POST /chat`** — two accepted callers, resolved by a new `require_chat_caller`
dependency in `app/channels/security.py`:

| Caller | Presents | Tenant + session come from |
|---|---|---|
| Widget (public) | a widget session token | **the verified token, never the body** |
| Server-to-server / `chat_cli` / tests | the existing `API_AUTH_TOKEN` bearer | the request body, as today |

`ChatRequest` loses `tenant_id` and `widget_key` from the *public* path entirely.
This closes defect 3 and finally makes `widget/README.md`'s claim true. The existing
`test_chat_requires_the_bearer_token_when_one_is_configured` keeps passing because
the trusted path is unchanged.

**Event filtering** — an explicit allowlist reaching the browser: `token`,
`acknowledgement`, `suggestions`, `handoff`, `final`, `error`. `tool_start` /
`tool_result` are logged only, exactly as `vapi_llm.py:141-143` does (closing
defect 4). Add the same three-invariant docstring the voice channel carries, since
chat now has the same obligations.

`_events()` also gains a **heartbeat** (`: ping\n\n` every ~15s while the brain is
thinking) so proxies don't kill an idle SSE connection mid-tool-call.

### Step 4 — CORS (`app/main.py`)

Add `CORSMiddleware` scoped to the chat routes, permissive on origin. This is safe
*because* the session token is the real boundary and no cookies are involved — the
per-tenant origin allowlist is enforced where it actually matters, at the handshake.
Preflight (`OPTIONS`) must return 200 with `Access-Control-Allow-Headers` covering
`authorization, content-type`.

Also mount two routes for the bundle:
* `GET /widget.js` → `FileResponse(widget/dist/widget.js)`, long `Cache-Control`
  with the build hash as an ETag.
* `GET /widget/demo` → a small self-contained HTML page embedding the widget against
  a local tenant. This is what makes the widget developable without a client site.

A missing bundle returns 404 with a clear message rather than a 500.

### Step 5 — `suggestions` event + the one emergency-path change

`app/tools/booking_tools.py` — `check_availability` becomes
`@tool(response_format="content_and_artifact")`. The returned **text is byte-identical
to today**; only the artifact is added, so no prompt behaviour changes.

`app/brain/runner.py`:
* `EventType` gains `"suggestions"` (`runner.py:26-28`). `is_spoken` stays
  `("token", "acknowledgement")` — untouched.
* A `_suggestions_artifact(message)` helper beside the existing
  `_handoff_artifact` (`runner.py:231-244`), same shape.

**The one change touching the emergency path, called out on purpose:**
`_handoff_artifact` currently returns `None` unless `artifact["transfer"]` is truthy
(`runner.py:238-243`). On chat, `SmsCallbackEscalator.can_transfer` is `False`
(`app/tools/messaging/transfer.py:54`), so **`/chat` can never emit a `handoff`
today** and the click-to-call comment already in `chat.py:50-55` describes
unreachable code.

Fix: emit `handoff` whenever `kind == "handoff"`, carrying `transfer: bool` in
`data`, and move the decision to the channels — `app/channels/vapi_llm.py:136-140`
gates `pending_transfer` on `event.data.get("transfer")` before ever emitting a
`transferCall` frame. Net behaviour on voice is identical; chat gains a
click-to-call button. `tests/test_warm_transfer.py`'s "voice yields exactly one
handoff, chat none" assertion changes meaning and must be rewritten to assert the
thing that actually matters: **a `transferCall` frame is emitted on voice and never
on chat.** That is the real invariant; the old test was asserting a proxy for it.

### Step 6 — durable chat transcripts

`app/db/migrations/0006_chat.sql`:

* `chat_sessions` — `id`, `tenant_id`, `widget_key`, `origin`, `started_at`,
  `last_seen_at`, `ended_at`, `message_count`.
* `chat_messages` — `id`, `tenant_id`, `session_id`, `role` (`text` + `CHECK`, not a
  PG enum — the Phase 4 rule), `content`, `created_at`.
* Both: `enable` **and** `force row level security`, a `tenant_id`-scoped policy, and
  an explicit `grant` to `app_backend` — required to pass the existing migration lint
  in `tests/test_migrations.py`.
* A `pg_cron` prune in the same file, following `0004_retention.sql`, honouring
  `chat_transcript_retention_days`. Chat transcripts carry guest names and phone
  numbers, so this closes the same PII item plan §16 flags.

> **Naming:** the existing `messages` table is **outbound SMS**, not chat, despite
> plan §6b listing it as "chat transcripts". Note this in the migration header —
> it is a live trap for anyone reading the plan against the schema.

`app/db/store.py` — a `ChatLog` protocol with sync + async twins
(`record_chat_message` / `arecord_chat_message`, `astart_chat_session`,
`alist_chat_messages`), matching the convention documented at `store.py:8-17`.
Implemented on `InMemoryStore` and `SupabaseStore` (whose existing PostgREST rules
apply: `Prefer: return=representation`, never f-string a query, always
`tenant_id=eq.`).

Writes happen in `app/channels/chat.py` — user message at turn start, assistant text
on `final` — and are **wrapped so a store failure can never kill a live stream**,
the same reasoning as "never raise mid-stream" on voice.

### Step 7 — the widget (`widget/`)

```
widget/
  package.json  vite.config.ts  tsconfig.json
  src/main.ts        # entry: reads data-* attrs, mounts into Shadow DOM
  src/App.tsx        # launcher + panel + message list + composer
  src/useStream.ts   # fetch + ReadableStream reader (the real hotspot)
  src/QuickReplies.tsx
  src/api.ts         # /chat/session + /chat
  src/styles.css     # imported ?inline, injected into the shadow root
  dist/widget.js     # committed build output
  dist/.buildhash
```

Vite **library mode**, single IIFE, no code splitting, CSS inlined into the JS and
injected into the shadow root (avoids a plugin dependency and any external stylesheet).

Embed contract — frozen from here:

```html
<script src="https://host/widget.js"
        data-widget-key="pk_widget_hotelmzv_demo"
        data-accent="#0f766e"></script>
```

Behaviour: idempotent init (a script included twice mounts one widget), handshake on
first open (not on page load — no cost for visitors who never click), `session_id`
persisted in `sessionStorage` so a refresh keeps the conversation, streaming text
with a typing indicator, quick-reply chips from the handshake and from `suggestions`
events, a click-to-call button on `handoff`, and a spoken-style apology on `error`
rather than a dead panel.

`useStream.ts` is where the bugs will actually live: `EventSource` cannot POST, so
it's `fetch` + `response.body.getReader()` + `TextDecoder` + manual `\n\n` framing,
with `[DONE]` handling, mid-stream `error` events, and an `AbortController` on close.

**Guarding the committed bundle** — a stale `dist/` that doesn't match `src/` is the
classic failure of this approach, and git does not preserve mtimes. So the build
writes `dist/.buildhash` (sha256 over the sorted `src/` files), and a **pytest** test
recomputes it and fails on mismatch. The check lives in Python, so CI stays
pytest-only and nobody needs Node to catch the drift.

`infra/Dockerfile` gains `COPY widget/dist ./widget/dist` next to the existing
`COPY content ./content` — no Node in the image.

### Step 8 — docs
`widget/README.md` (rewrite: embed snippet, `data-*` reference, build commands,
the frozen-contract note), `content/README.md` (the new `chat` block per tenant),
`README.md` (widget row in the "four doors" table, update the stubbed/real table),
`CLAUDE.md` (Phase 5 done; new gotchas: the frozen embed contract, tenancy from the
token never the body, the `suggestions` artifact pattern, `messages` ≠ `chat_messages`),
and a plan §8 amendment recording the handshake design and the WhatsApp deferral.

---

## Testing

Everything stays offline on the existing `ScriptedChatModel` + `mock_http` +
autouse `no_network` fixtures.

**New:** `tests/test_widget_auth.py` (mint/verify round trip, expired token, tampered
signature, malformed input → `None` not an exception, missing secret still works);
`tests/test_chat_session.py` (handshake returns tenant display data + services;
unknown widget key → **404, not a broken stream**; origin allowlist enforced and
bypassed when empty; two handshakes get different `session_id`s);
`tests/test_chat_channel.py` (tenant comes from the token and a body `tenant_id` is
ignored; `tool_start`/`tool_result` never reach the client; `suggestions` carries
slots; `handoff` reaches chat; heartbeats don't corrupt framing);
`tests/test_chat_transcripts.py` (messages recorded per tenant; a store failure
does not break the stream); `tests/test_widget_bundle.py` (the `.buildhash` guard).

**Updated:** `tests/test_api.py::test_chat_can_book_through_the_http_channel` — it
currently asserts a `tool_start` payload appears (`test_api.py:108`), which is now
deliberately filtered; assert the booking landed in the store instead.
`tests/test_warm_transfer.py` — per Step 5, assert on the `transferCall` frame rather
than on handoff-event counts. `tests/test_migrations.py` picks up `0006_chat.sql`
automatically.

**Manual — the Phase 5 acceptance criterion:**

1. `npm --prefix widget install && npm --prefix widget run build`
2. `uvicorn app.main:app --reload` → open `http://localhost:8000/widget/demo`
3. Click the launcher → greeting + service chips appear (handshake worked).
4. "do you have a room tonight?" → streaming reply, then **slot chips**; click one →
   it books. Confirm the same `jobs` row via `python -m scripts.chat_cli` `/jobs` —
   *identical to the phone*, which is the criterion.
5. Refresh the page mid-conversation → the thread continues (`sessionStorage`).
6. Open the demo from a **second origin** (another `http.server` port) → works with
   an empty allowlist, 403s once `chat.allowed_origins` is set.
7. Two browsers on the same tenant → two `session_id`s, no shared history
   (closing defect 2).
8. With Supabase configured: `chat_sessions` / `chat_messages` rows appear, scoped to
   the right tenant.
9. `curl` `/chat` with a body `tenant_id` of another tenant → ignored.

## Risks

1. **Committed `dist/` drifts from `src/`.** Closed by the `.buildhash` pytest guard —
   the only mitigation that works without putting Node in CI.
2. **The embed contract changing after a client has pasted it.** Mitigated by freezing
   it in Step 7 and treating the bundle as the only replaceable part.
3. **`check_availability` becoming `content_and_artifact`.** Verified safe by the
   `escalate` precedent, but it is a critical-path tool — the returned *text* must stay
   byte-identical, and the existing tool tests must pass untouched. If they don't, stop.
4. **The `handoff` gating move (Step 5) touches the emergency path.** The guard is that
   a `transferCall` frame must still appear on voice and never on chat; that is now
   asserted directly rather than via a proxy.
5. **Origin allowlist empty by default.** Deliberate for dev, and documented in
   `content/README.md`, but a production tenant should set it. Log a warning once per
   tenant at handshake when it's empty.
6. **No rate limiting** (your call, deferred to phase10). Until then `/chat` is
   billable by anyone holding a widget key, against Groq's ~76-request daily free cap.
   The session token at least makes per-session accounting possible later without
   another contract change.
7. **SSE through proxies.** `X-Accel-Buffering: no` is already set (`chat.py:35`); the
   heartbeat in Step 3 covers idle timeouts a header can't.

## Deferred

Per-key rate limiting and usage tiers → `plans/phase10.md` (with the subscription /
bring-your-own-LLM decision behind it). WhatsApp → phase10 item 4. Widget i18n,
file upload, and a Phase 8 avatar pane — the bundled build exists so these are
additive. Reading tenant chat config from Supabase rides along with the existing
`TENANT_SOURCE` flip (phase10 item 8).

## Est. effort

3–4 days. Slightly over plan §15's 2–3 because the plan assumed only "an SSE endpoint
+ a widget" and this also closes six pre-existing `/chat` defects and adds transcript
persistence. Steps 1–6 are Python and mechanical; Step 7 is where the time goes, and
`useStream.ts` is the part worth writing carefully.
