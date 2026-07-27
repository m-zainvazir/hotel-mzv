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
**Phase 5 (Chatbot), Phase 6 (MCP), Phase 7 (Deploy) and Phase 8 (Avatar)
are not backlog items** — they're substantive planned phases in their own
right (plan §15) and stay exactly where they are in the roadmap. Nothing
here is required before any of them can start.

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
per-visitor widget sessions — this needs the Phase 8 admin dashboard's
authentication surface, not something to build standalone. Two risks are
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
likely rides along with the Phase 8 admin-dashboard decision rather than
standing alone. Effort: medium–large — a new tenant-facing auth surface plus
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

### 8. Flip the tenant *read* path to Supabase

`tenants`/`services` tables exist and `scripts.sync_tenants` keeps them
current, but the brain still reads `content/tenants/*.json` — deliberate,
per `plans/phase4.md`'s "On the tenant read path" note (the test suite's
`no_network` fixture ordering needed the JSON path to stay default). Flip is
one setting, `TENANT_SOURCE=supabase` — but needs its own live-verification
pass first (tenant resolution by phone/assistant-id querying Supabase
instead of files, cache behaviour, `no_network` guard interaction). Effort:
small–medium — mostly re-verification, the write path is already proven.

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

---

## Explicitly not in scope here

Avatar (Tavus/Simli) is Phase 8, not a backlog item — see plan §16 and
`AI-Receptionist-Build-Plan.md` for that decision when it's time. An admin
dashboard is likewise associated with Phase 8 territory in `plans/phase4.md`'s
own deferred list. MCP server wiring is Phase 6 in full, not a stray loose
end — the schema/model placeholders already in place
(`app/tenancy/models.py`, `app/db/migrations/0001_schema.sql`) are exactly
what that phase will build on, not something to pick up piecemeal here.
