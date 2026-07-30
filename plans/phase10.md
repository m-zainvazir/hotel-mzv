# Phase 10 — Optional / deferred backlog

## Context

Phases 1–4 are done. Building any of Phases 1–4 turned up a steady trickle of
loose ends that were each individually correct to defer — not required for
the phase they came up in, not blocking anything downstream, usually waiting
on either an external input from the client or simply not being worth the
risk/effort until something concrete needs it. Left scattered across
`CLAUDE.md`, `README.md`, and the individual phase plans, they're easy to
lose track of.

This is not a phase in the executable sense — there's no acceptance
criterion, no fixed order, no single feature it delivers. It's a parking lot:
one place to find every "come back to this eventually" item, so picking one
up later doesn't mean re-deriving why it was deferred in the first place.
**Phase 5 (Chatbot), Phase 6 (MCP), Phase 7 (Deploy) and Phase 8 (Analytics +
admin) are not backlog items** — they're substantive planned phases in their
own right (plan §15) and stay exactly where they are in the roadmap. Phase
8's scope narrowed by client decision (the video avatar moved here — item
13 — and it's genuinely well-timed, since plan §12's premise for it turned
out to be stale regardless). Nothing here is required before any of them
can start.

Pick items up independently, in any order, whenever the input they're
waiting on arrives or the itch to build them shows up.

---

## Needs an external input from you

### 1. Actually clone a voice (Phase 4 leftover)

Everything except the clone itself is done and live-verified: `app/tenancy/voice.py`
(the Cartesia client + consent gate), the `voice_consents` table, and the DB
trigger (`0005_voice_consent.sql`) that refuses to let a `voice_id` be set
without a recorded consent row — tested offline and proven live against the
real Supabase project.

**What's needed:** a clean 15–30 second recording of the voice to clone
(quiet room, phone voice memo is fine) and one signed, dated line of consent
— even for your own voice, CLAUDE.md convention #6 has no exceptions.

**Then:**
```
python -m scripts.onboard_tenant --config content/tenants/<id>.json \
    --voice-sample sample.wav --consent-url https://.../consent.pdf \
    --consent-owner "Name" --consent-granted-by "Name"
python -m scripts.provision_vapi --tenant <id>   # bake the new voice_id into Vapi
```
Effort: small (~30 min) once the recording exists — it's a single command.

### 2. A second Cal.com account (Phase 4 leftover)

Per-tenant Cal.com credentials are live and *resolution* is proven — two
tenants demonstrably get handed different API keys
(`app/tenancy/secrets.py`, live-verified in `plans/phase4.md`'s record). What
hasn't been proven is a full booking landing in two genuinely different
calendars end-to-end, because only one real Cal.com account (`hotel-mzv`'s)
exists to test against.

**What's needed:** a second Cal.com account + API key, and one event type on
it (same checklist as `hotel-mzv`'s — see `content/README.md`: multiple
durations enabled, auto-confirm on).

**Then:** `scripts.onboard_tenant --calcom-api-key <the new key>` against a
second tenant, book once through `chat_cli`, confirm it landed on the right
calendar. Effort: small (~1 hour, mostly Cal.com dashboard setup).

### 3. Turn Twilio SMS on (parked by client decision)

`TwilioNotifier` and `WarmTransferEscalator` are fully implemented and
tested against mocks (`tests/test_twilio_notifier.py`) — this was a client
decision to leave off, not a technical gap.

**What's needed:** the client's go-ahead, plus (if US) A2P 10DLC
registration started early — it takes days, not minutes, and unregistered
long-code SMS is silently filtered while Twilio still returns 201.

**Then:** flip `notifications.provider` from `"stub"` to `"twilio"` in the
tenant JSON, set `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`/a sender in
`.env` (or per-tenant via `onboard_tenant --twilio-*`, live-verified in
Phase 4). One JSON edit — see `content/README.md`. Effort: none, already
built; this is purely a go/no-go.

### 4. WhatsApp chat (pending decision, plan §16)

Never decided: web-only vs. WhatsApp too. `TWILIO_WHATSAPP_FROM` exists as a
config field (`app/config.py`) but nothing reads it yet. Plan §8 already
covers the design ("optional WhatsApp via Twilio uses the same endpoint").

**What's needed:** a decision from the client on whether this is in scope,
and if so, a Twilio WhatsApp sender. Effort: medium — real work, not a flag
flip (a genuine second entry point into `/chat`).

### 11. A concrete search/scraper MCP server (Phase 6 leftover)

The MCP loader (`app/mcp/`) is vendor-neutral by design — proven against a
first-party demo server (`scripts/demo_mcp_server.py`) and live-tested
against a real Tavily connection during Phase 6 development — but no tenant
has a *committed*, ongoing third-party server. `hotel-mzv`'s Tavily entry so
far has been a local testing convenience, not a production configuration.

**What's needed:** an API key from Tavily, Firecrawl or Exa (whichever you
prefer — free tiers exist on all three).

**Then:**
```
python -m scripts.register_mcp_server --tenant <id> --name <name> \
    --url '<vendor url>?apiKey=${secret}' --secret <the real key>
```
One command, no code change — see `content/README.md`'s "Connecting any
remote MCP server" section. Effort: trivial once the key exists.

### 12. Self-serve MCP server registration via a tenant-facing UI (undecided)

Not decided: whether tenants should add their own MCP servers (a CRM, a
search tool) through a UI, rather than you running
`scripts/register_mcp_server.py` on their behalf. The storage layer already
fits this without redesigning it — the `mcp_servers` table's real RLS
(`tenant_id = auth.jwt() ->> 'tenant_id'`) already enforces "a tenant can
only touch its own rows" at the database layer, the same pattern
`app/tenancy/secrets.py`'s `get_tenant_secret` RPC already uses (derives
`tenant_id` from the caller's own JWT claim, never a parameter) — a
self-serve UI could sit directly on top of that.

What's actually missing is bigger than a form: there's no tenant-facing
login at all today, only a shared admin bearer token and anonymous
per-visitor widget sessions — this needs item 14's tenant-login surface
first, not something to build standalone. Two risks are
specific to *this* feature, not general Phase 8 risk: (1) `set_tenant_secret`
is deliberately backend-only today, taking `tenant_id` as a parameter
because only the trusted admin path calls it — a tenant-scoped write variant
would need to derive `tenant_id` from the JWT the same way the read-side RPC
already does, or one tenant could plausibly overwrite another's secret; (2) a
tenant submitting an arbitrary server URL opens SSRF (e.g. a URL pointing at
`169.254.169.254` or an internal service) — today that risk doesn't exist
because only a trusted operator ever types these URLs in. Any such UI should
also hard-remove the `stdio` transport option entirely rather than leaving
it default-off (`MCP_ALLOW_STDIO`), since a tenant-submitted subprocess
command is remote code execution, full stop.

**What's needed:** a decision on whether this is worth building at all —
rides along with item 14 (real per-tenant login) rather than standing
alone. Effort: medium–large — a new tenant-facing auth surface plus
a new tenant-scoped secret-write RPC, not a form.

---

## No external blocker — pick up whenever there's time

### 5. `cancel` / `reschedule` as native tools

`BookingProvider.cancel`/`.reschedule` are implemented for both providers
(`app/tools/booking/calcom.py`, `stub.py`) and marked ⚠️ VERIFY, but **no
LLM-facing tool wraps them** — `NATIVE_TOOLS` (`app/tools/registry.py`) only
has the original five. A caller today can't ask the bot to move or cancel a
booking. Deferred twice already (`plans/phase3.md`, `plans/phase4.md`).
Effort: medium — a tool wrapper + prompt copy + graph wiring + tests, same
shape as the existing five tools.

### 6. Booking idempotency keys

Flagged as a risk since Phase 3: a timed-out `POST /bookings` may have
actually succeeded on Cal.com's side, leaving a real calendar event with no
local `jobs` row. `metadata.job_id` exists as a reconciliation handle but
nothing automatically reconciles yet. Effort: small–medium — needs a
decision on the reconciliation strategy (poll Cal.com on ambiguous timeout?
a periodic sweep?) more than a lot of code.

### 7. Google Calendar as a second `BookingProvider`

`app/tools/booking/google.py` is a fully scaffolded interface where every
method raises `NotImplementedError`. Cal.com is live and working; this is
only worth building if a specific client needs Google Workspace-native sync
instead. Effort: medium — the interface is proven (Cal.com's implementation
is the template), but Google OAuth consent-screen verification is its own
small project.

### 8. Flip the tenant *read* path to Supabase — done in Phase 8

Built, tested and code-complete — `app/tenancy/supabase_repository.py`'s
`SupabaseTenantRepository` (a boot-time snapshot + background refresh, never
per-request I/O, since `TenantRepository.get()` is synchronous) — see
`plans/phase8.md`. The `no_network` fixture-ordering concern this item was
originally deferred over is confirmed fixed (`tests/conftest.py` declares
`no_network` before `isolated_runtime`, and `isolated_runtime` now resets
`loader._repository` between tests). `TENANT_SOURCE=supabase` still isn't
the *default* (dev/test stay on `"json"`, zero behaviour change), but it's
now required whenever the Phase 8 admin panel is turned on
(`ADMIN_ENABLED=true` without it fails production preflight — see "the
phantom edit" in `plans/phase8.md`). What's left is exactly the live
verification this item used to describe: applying `0008_analytics.sql` +
`0009_admin.sql` to the real project and confirming the flip against it
(`plans/phase8.md`'s Step 10) — not additional code.

### 9. Prove Vapi warm transfer with a real phone call

Warm transfer is implemented and covered by `tests/test_warm_transfer.py` +
`provision_vapi --dry-run`, but per `plans/phase3.md` Risk 7, it has never
been proven with an actual live transferred call — the destination numbers
used so far are placeholders. Cheap to verify whenever there's a real
on-call number to transfer to: dial in, trigger an emergency phrase, confirm
the transfer actually connects. Effort: trivial, just needs a live number
and five minutes.

### 10. Mid-call resume (`caller` / `booking_draft` state fields)

`ReceptionistState` declares `caller` and `booking_draft` (plan §5) but no
node writes them yet — caller details currently live implicitly in the
message history. The intended use is resuming a dropped call without asking
the caller to repeat themselves. Only worth building once dropped-call
resume is an actual observed problem, not a hypothetical one. Effort:
medium — needs a node that populates them and a strategy for reattaching a
resumed call to the right thread.

### 13. The video avatar (moved out of Phase 8)

Phase 8 shipped analytics and per-tenant admin; the avatar was deliberately
left out — wanted, later. Two things changed since plan §12 was written.

**§12 is stale on the key point.** It recommends *"Tavus (already integrated
with Vapi)"* — **Vapi discontinued its Tavus integration** (Vapi staff, 20
Jun 2025). There is no longer a provider-side toggle that adds a face to a
call. The workable path is now **browser-side**: Vapi's web SDK exposes a
public `getDailyCallObject()` and emits a `'video'` event carrying a
`MediaStreamTrack`, and **Simli** mints a short-lived session token
server-side (`POST /compose/token`, the startAudioToVideoSession call, from
an API key plus a `faceId`) which the browser then uses over WebRTC. The
avatar is a composition the *client* performs, not an add-on the
orchestrator provides. Cost ≈ **$0.05/min**, free tier $10 signup credit +
50 min/month — enough to prove the path without a commitment.

**A familiar trap is already in the repo.** `.env.example` has carried
`TAVUS_API_KEY=` / `SIMLI_API_KEY=` placeholders since Phase 0, but neither
is a real `Settings` field and `model_config` has `extra="ignore"` — so they
are silently dropped exactly the way `LANGCHAIN_*` was for six phases
(CLAUDE.md's tracing gotcha). Making `simli_api_key` a real field is the
first line of work.

Both delivery modes are wanted, and they're genuinely different products:

- **Avatar mode inside the existing chat widget**, gated per tenant and
  toggleable from the Phase 8 admin panel. A new `TenantConfig.avatar`
  sub-model (`enabled`, `provider: Literal["simli"]`, `face_id`, `mode:
  Literal["widget","embed","both"]`), surfaced in `/chat/session`'s response
  so the widget knows whether to render the button, and rendered as a form
  section in `admin/src/views/Config.tsx`. The frozen `<script
  data-widget-key>` contract is untouched — the widget just learns a new
  capability from the handshake it already performs.
- **A separate avatar-only embed/demo page** for users who should get
  voice/avatar and no text chat. A full page, not an embed, so it can't live
  in `widget/`'s library-mode IIFE bundle. Ride it on the build system
  Phase 8's `admin/` app established rather than adding a third toolchain.

Server-side, one new endpoint: **`POST /avatar/session`**, minting the Simli
token with `SIMLI_API_KEY` **server-side only** (never shipped to the
browser), authenticated by the same widget session token `/chat` already
accepts, refusing when `tenant.avatar.enabled` is false, and rate-limited —
per-minute billing on a public endpoint is a spend hole. **The brain is
untouched throughout** (convention #4): a presentation layer over the same
graph, voice and tools.

**What's needed:** a Simli account and API key, plus a chosen `faceId`
(their library, or a custom face — which has its own upload +
likeness-consent step, and convention #6's reasoning about voice applies
just as squarely to a face). Also a decision on which tenants get it, since
it's metered per minute and is the obvious paid add-on.

**Then:**
```
# 1. make the key a real Settings field, add TenantConfig.avatar
# 2. POST /avatar/session mints the Simli token server-side
# 3. widget: getDailyCallObject() -> 'video' track -> Simli WebRTC composition
# 4. toggle it on for one tenant from /admin, confirm the button appears
python -m scripts.chat_cli --tenant <id>   # unchanged: the brain never learns an avatar exists
```
⚠️ VERIFY before building: the exact Simli token endpoint path and payload
shape; whether `getDailyCallObject()` is stable across the Vapi web SDK
version in use; and whether the `'video'` event's track is already
audio-driven or needs a separate audio tap to feed Simli. All three read
fine in docs and behave differently in a browser.

Effort: medium — 2–3 days, most of it in the browser and most of *that*
getting two WebRTC sessions to agree on timing. The server side is one
endpoint and one config field.

---

## No external blocker, but a decision needed — the tenant-login track

### 14. Real per-tenant login (Supabase Auth)

Phase 8 shipped the admin surface "operator-only now, designed for tenant
login later", and the *later* half is genuinely pre-built rather than
merely promised. Already in place: `AdminPrincipal` and
`require_admin`/`require_tenant_access` (`app/channels/admin_auth.py`), with
every admin route depending on the latter and none on the raw token; the
tenant id in the URL path, so authorization is one comparison in one
dependency; `GET /admin/api/session` returning `{kind, tenant_ids,
capabilities}` that the UI already branches on; `_OPERATOR_ONLY_PATHS` in
`app/tenancy/admin.py`, enforced today against a set every current
principal satisfies; and — the load-bearing one — **every analytics read
already goes through the tenant-scoped JWT**
(`app/db/auth.py::tenant_jwt`, `security_invoker` views, an RPC deriving its
tenant from the JWT rather than a parameter), so a logged-in tenant reading
its own metrics runs the identical code path an operator does.

What's missing is one branch in `require_admin` and a user→tenant mapping.

The mapping is the only real design question: **a GoTrue JWT carries no
`tenant_id` claim by default.** Two options — a custom access-token hook
injecting the claim (Supabase-version-dependent, ⚠️ VERIFY availability on
the project's plan), or an `admin_users (user_id uuid, tenant_id text, role
text)` table the backend reads. **Prefer the table**: it's a migration in
this repo rather than a dashboard setting nobody can diff, it supports one
user administering several tenants, and it makes the operator/tenant
distinction a row rather than a magic claim.

One rule is non-negotiable and is why this stays a backend concern: **the
browser must never hold a JWT that PostgREST accepts.** It talks to
`/admin/api`; the backend verifies the GoTrue token (verify, don't decode —
`widget_auth.py` is the only verification code in the repo today and it's
HMAC; GoTrue signs HS256 against `SUPABASE_JWT_SECRET` on legacy projects
and ES256/JWKS on newer ones, ⚠️ VERIFY which applies) and then mints its
own `app_backend` tenant JWT for the data reads. That is exactly what
`0002_rls.sql`'s `app_backend`-not-`authenticated` choice exists to
preserve. Letting the browser use supabase-js with the user's own token is
the one-line shortcut that undoes it.

**What's needed:** a decision that tenants get logins at all (it changes the
product from "we run it for you" to "you have an account"), Supabase Auth
enabled on the project, and a call on invite-only vs. self-serve signup —
the former is right for a handful of pilot clients and avoids a whole
email-verification surface.

**Then:**
```
# 1. 00NN_admin_users.sql — user_id -> tenant_id mapping, RLS'd like everything else
# 2. require_admin gains a second branch: verified GoTrue JWT -> AdminPrincipal(kind="tenant")
# 3. admin/src: a login view; the rest of the UI already branches on /admin/api/session
# 4. _OPERATOR_ONLY_PATHS stops being inert — no route changes needed
```
Landing this also unblocks **item 12** (self-serve MCP server registration),
which was deferred specifically for want of a tenant-facing auth surface —
and it inherits that item's two documented risks unchanged:
`set_tenant_secret` needs a tenant-scoped write variant deriving `tenant_id`
from the JWT the way `get_tenant_secret` already does, and a
tenant-submitted server URL is an SSRF vector that doesn't exist while only
a trusted operator types them.

Effort: medium — 2 days for auth plus the mapping table, another for the
login UI and session handling. Small precisely because Phase 8 paid the
design cost up front; it would be a rewrite otherwise.

---

## Explicitly not in scope here

MCP server wiring is Phase 6 in full, not a stray loose end — the
schema/model placeholders already in place (`app/tenancy/models.py`,
`app/db/migrations/0001_schema.sql`) are exactly what that phase will build
on, not something to pick up piecemeal here. Phase 8 (analytics + per-tenant
admin) is likewise a substantive planned phase in its own right, not a
backlog item — done, see `plans/phase8.md`; only its live-verification step
and the two items above (avatar, tenant login) remain here.
