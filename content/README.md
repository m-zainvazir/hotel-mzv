# content/ — everything you edit to change the bot

This is the one folder for tuning behaviour and business details **without
touching code**. Edits take effect on the next message — no restart needed.

| File | What it controls | Example edit |
|---|---|---|
| `system-prompt.md` | **How the bot thinks and talks** — its rules, tone, booking flow, safety handling. Sent to the AI on every turn. | Make it stricter, friendlier, change the booking steps |
| `tenants/hotel-mzv.json` | **What business it is** — name, greeting, services, prices, hours, emergency words, phone/voice. One file per client. Currently a hotel front desk (rooms, restaurant, spa, event space, airport transfer). | Add a service, change opening hours, reword the greeting |
| `tenants/northside-plumbing.json` | A second example business (a plumber). | — |
| `acknowledgements.json` | The little "one second…" lines said **while a tool runs**, so callers don't hear silence. A `<tool>.<channel>` key (e.g. `escalate.voice`) is tried before the bare `<tool>` key — use it for a line that's only true on one channel. | Reword them, add more |
| `tenants/<id>.json` → `chat` block | **The widget's own display config** (Phase 5) — `accent_color`, `launcher_label`, `quick_replies` (on/off), an optional widget-specific `greeting` override, and `allowed_origins`. Never reaches the graph; the brain doesn't know a widget exists. | Rebrand the launcher color, restrict a widget key to the client's own domain |
| `tenants/<id>.json` → `mcp_servers` block | **Which MCP servers this tenant can use** (Phase 6) — a CRM, a search tool, an internal API. In production, prefer `scripts/register_mcp_server.py` instead (see below) — no redeploy needed. | Give a tenant a search tool |

## The two placeholders you'll meet

- **`system-prompt.md`** uses `${...}` slots (e.g. `${business_name}`,
  `${services}`, `${business_hours}`). The code fills these from the tenant's
  JSON each turn. Leave the `${...}` names as they are; edit the words around
  them. An unknown `${name}` is left as-is rather than crashing, so a typo is
  safe.
- **`tenants/*.json`** is plain configuration. `greeting` is spoken instantly at
  the start of a call (no AI, for zero delay); everything else feeds the prompt.

## What is NOT here (on purpose)

- **Which AI model + API keys** → `.env` at the project root
  (`LLM_PROVIDER`, `GOOGLE_MODEL`, `GOOGLE_API_KEY`, …). Check the active model
  any time with `python -m scripts.check_model`, or read it off the chat banner.
- **The conversation logic / wiring** (the "brain") → `app/brain/`. That's code,
  not content — you shouldn't need to touch it to reconfigure the bot.

## After editing a tenant file

If you changed anything under `vapi` / `voice` **or** `emergency.escalation_phone`
/ `emergency.allow_warm_transfer` (Phase 3 warm transfer needs re-provisioning
whenever the transfer number changes — see below) and you're using phone or
web calls, push it to Vapi:

```
python -m scripts.provision_vapi --tenant hotel-mzv
```

Plain chat needs nothing — just send the next message.

## Syncing a tenant file to Supabase, and which one is actually "true" (Phase 4 → Phase 8)

```
python -m scripts.sync_tenants --tenant hotel-mzv    # one tenant
python -m scripts.sync_tenants                        # every tenant
```

**Read this before editing a tenant file if `ADMIN_ENABLED=true` anywhere.**
Whether these JSON files are still the bot's actual source of truth depends
entirely on `TENANT_SOURCE` in `.env`:

- **`TENANT_SOURCE=json` (dev/local default).** These files are exactly what
  you think they are — edit one, the next turn uses it, no restart. Running
  `sync_tenants` afterward is optional bookkeeping (it pushes a copy into
  Supabase's `tenants`/`services` tables so onboarding/audit data stays
  current), and skipping it costs nothing: nothing reads those tables in
  this mode.
- **`TENANT_SOURCE=supabase` (what production needs once the Phase 8 admin
  panel is turned on).** These JSON files become **seed data and a
  degraded-mode fallback only** — the running app loads its tenant registry
  from Supabase at boot and serves *that*, refreshing on an admin panel save
  and periodically in the background (`TENANT_SNAPSHOT_REFRESH_SECONDS`).
  Editing a JSON file in this mode does **nothing** until you run
  `sync_tenants` to push it — and even then, whoever's driving the admin
  panel might not know you did, and vice versa.

**The trap to avoid — don't "fix" a panel edit by running `sync_tenants`.**
An operator changes a tenant's greeting in the admin panel; running
`sync_tenants` afterward (because the JSON file "looks stale" or out of
habit) blind-upserts the *old* JSON content back over Supabase, silently
reverting the panel's edit. `sync_tenants` refuses to run against a
`TENANT_SOURCE=supabase` project without `--force`, precisely so this isn't
an accident — if you genuinely mean to overwrite live config with what's on
disk, `--force` says so out loud. Going the other direction — pulling live
config back down into the JSON files so they stop being stale, e.g. before a
commit — is `sync_tenants --export`.

Either way, `sync_tenants` needs `SUPABASE_URL` + `SUPABASE_SECRET_KEY` in
`.env`. See `plans/phase8.md` ("the phantom edit" / "the sync stomp") for the
full reasoning.

## Giving a tenant its own Cal.com / Twilio credentials (Phase 4)

Every tenant on `"calcom"` shares one `CALCOM_API_KEY` from `.env` by
default — fine for a single client, wrong the moment two tenants have their
own real Cal.com accounts. Give a tenant its own credentials with:

```
python -m scripts.onboard_tenant --config content/tenants/<id>.json \
    --calcom-api-key cal_live_... \
    --twilio-account-sid AC... --twilio-auth-token ...
```

This writes the credential into Supabase Vault, scoped to that tenant only —
nothing changes in the tenant's JSON file or `.env`. A tenant with no
per-tenant credential keeps using the shared `.env` one automatically; there's
no flag to flip. (This is also how a brand-new tenant gets onboarded end to
end — see `python -m scripts.onboard_tenant --help`.)

## The chat widget's own settings (Phase 5)

`widget_keys` (top-level on the tenant, already existed) is the public key a
client's site embeds — see `widget/README.md` for the `<script>` tag. The
`chat` block controls how the widget itself looks and behaves once a visitor
opens it:

```jsonc
"chat": {
  "allowed_origins": [],              // [] = any origin may use this widget key
  "accent_color": "#0f766e",
  "launcher_label": "Chat with us",
  "quick_replies": true,
  "greeting": null                    // null = fall back to the tenant's own "greeting"
}
```

None of this reaches the graph — the brain has no idea a widget exists.
`allowed_origins` is the one field worth setting for a real client: leaving
it empty (the default, convenient for local development) means any site that
gets hold of the widget key can embed it; a production tenant should list its
real domain(s) (`["https://example-hotel.com"]`). Checked at the
`/chat/session` handshake, not at every `/chat` call — the handshake is what
mints the session token a widget then presents.

No re-provisioning needed for any of this — unlike `vapi`/`voice`/the
emergency phone, the widget reads its config fresh on the next `/chat/session`
call, same as everything else that's plain chat.

## Going live: Cal.com booking + Twilio SMS (Phase 3)

`hotel-mzv` is already live on Cal.com — `booking.provider: "calcom"`,
`booking.event_type_id: 6446177`, verified against a real account. Both
tenants' `notifications.provider` is still `"stub"` (SMS parked by client
decision, not a technical gap). Flipping any tenant's provider is a JSON
edit, not a code change:

```jsonc
"booking": {
  "provider": "calcom",          // was "stub"
  "event_type_id": 1234567,      // your Cal.com event type id
  ...
},
"notifications": {
  "provider": "twilio",          // was "stub"
  ...
}
```

Then set `CALCOM_API_KEY` / `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` (+ a
sender: `TWILIO_FROM_NUMBER` or `TWILIO_MESSAGING_SERVICE_SID`) in `.env` at
the project root — never in a tenant file. That's the *shared* credential
every tenant falls back to; give a specific tenant its own account instead
via `scripts.onboard_tenant` (see above).

**Cal.com event type checklist** (learned setting up `hotel-mzv`'s real one):
- **Enable multiple durations** if one event type serves several services
  with different lengths (`PATCH /v2/event-types/{id}` with
  `lengthInMinutesOptions: [...]` — no dashboard click-through needed).
  Without it, Cal.com 400s any booking that specifies a length, and a tenant
  can only book its one fixed duration.
- **Set it to auto-confirm.** A "requires confirmation" event type lets us
  say "booked" before a human has actually accepted it.
- The attendee email we synthesize for callers who don't give one
  (`caller-<digits>@example.com`) must resolve — Cal.com checks real
  deliverability, not just syntax. Don't repoint
  `BOOKING_PLACEHOLDER_EMAIL_DOMAIN` at a domain that doesn't exist.

**Once a tenant is on `"calcom"`, Cal.com owns availability — not this
file.** `booking.hours`/`lead_time_hours`/`slot_granularity_minutes` become
prompt copy only; the real schedule lives on the Cal.com event type. Keep
them in sync by hand, or the bot will describe hours it can't actually book.

**Warm transfer** (voice only) is on by default whenever a tenant has an
`emergency.escalation_phone`. Set `emergency.allow_warm_transfer: false` to
keep a tenant on SMS-alert-only instead of a live transfer. Either way,
re-run `provision_vapi` after changing the number — Vapi only transfers to
numbers declared at provisioning time.

## Booking via MCP instead of REST (Phase 9 Part A)

`booking.provider: "calcom"` talks to Cal.com's REST API directly
(`app/tools/booking/calcom.py`). `booking.provider: "mcp_calcom"` is the same
booking behaviour reached through Cal.com's **official hosted MCP server**
(`https://mcp.cal.com`) instead — same event type resolution, same
placeholder email, same error handling, same local `jobs` row staying
authoritative. Nothing about what the bot says or does changes; only the
transport underneath `check_availability` / `book_job` does. See
`app/tools/booking/mcp_calcom.py`'s module docstring for exactly what's
reused verbatim from the REST provider.

Unlike the long-tail MCP servers below, this is **not** something you add to
a tenant's `mcp_servers` array — `booking.provider` is the whole switch, and
Cal.com is never exposed to the model as a callable tool (plan §9 explains
why: the widget's slot chips, the booking-specific acknowledgement line, and
`send_confirmation`'s job lookup all depend on the provider-layer boundary
staying where it is).

**One-time setup per tenant**, because Cal.com's hosted MCP server is OAuth
2.1 only — no static API key path exists for it:

```
python -m scripts.authorize_calcom --tenant hotel-mzv
```

This opens a browser, walks a normal Cal.com login + consent screen for
**that tenant's own account**, and stores the resulting refresh token +
OAuth client credentials in Supabase Vault, scoped to that tenant
(`calcom_mcp_refresh_token` / `calcom_mcp_client_id` / `calcom_mcp_client_secret`).
Needs `SUPABASE_URL` + `SUPABASE_SECRET_KEY` in `.env`, same as
`onboard_tenant`. Nothing else runs it again unless the grant is revoked —
`app/mcp/oauth.py::access_token_for` refreshes headlessly from here on,
called by `McpBookingProvider` on every turn.

Then flip the provider — same JSON shape `booking.event_type_id` already
uses, just a different `provider` string:

```jsonc
"booking": {
  "provider": "mcp_calcom",      // was "calcom"
  "event_type_id": 6446177,      // unchanged — Cal.com still needs this
  ...
}
```

**Cal.com still owns availability either way** — the same "hours/lead-time
become prompt copy only" rule the plain `"calcom"` section above describes
applies identically here; the MCP hop doesn't change what governs the real
schedule.

Reverting a tenant is the same one-word JSON edit back to `"calcom"` —
`app/tools/booking/calcom.py` is untouched by any of this and stays the
fallback.

## Connecting any remote MCP server (Phase 6)

Set `MCP_ENABLED=true` in `.env` first — off by default, so this costs
nothing until you turn it on. Two ways to give a tenant a server, matching
`MCP_SOURCE` in `.env` (`json` by default, `supabase` in production — see
`.env.example`):

**`MCP_SOURCE=json` (dev/local)** — edit the tenant's `mcp_servers` array
directly, same hot-reload as everything else here:

```jsonc
"mcp_servers": [
  { "name": "tavily", "transport": "http",
    "url": "https://mcp.tavily.com/mcp/?tavilyApiKey=${secret}",
    "auth_secret_ref": "TAVILY_API_KEY" }
]
```

**`MCP_SOURCE=supabase` (production)** — don't edit the JSON; use
`scripts/register_mcp_server.py` instead. It writes straight to the live
`mcp_servers` table and Vault, so a new server reaches the tenant on its very
next turn — no redeploy, unlike everything else in this file:

```
python -m scripts.register_mcp_server --tenant hotel-mzv --name tavily \
    --url 'https://mcp.tavily.com/mcp/?tavilyApiKey=${secret}' \
    --secret tvly-xxxxx
python -m scripts.register_mcp_server --tenant hotel-mzv --list
python -m scripts.register_mcp_server --tenant hotel-mzv --name tavily --disable
```

**Two things worth knowing before pointing this at a real server:**

- **`${secret}` is a placeholder, substituted at connect time from Vault —
  never put a real credential in the JSON file or the table.** Real hosted
  MCP servers don't agree on where the credential goes: Tavily's takes it as
  a **URL query parameter** (`?tavilyApiKey=${secret}`, as above); most
  others want a header instead —
  `"headers": {"Authorization": "Bearer ${secret}"}`. Either way, `${secret}`
  gets substituted from whatever `--secret` (or the tenant's own
  `auth_secret_ref`) resolved to. Leave `headers` empty with an
  `auth_secret_ref` set and it defaults to a bearer header for you.
- **A server's `name` becomes part of every tool name it offers** (e.g.
  `tavily_search`), so it must be lowercase, `a-z0-9_-` only, and under 32
  characters — this is enforced at config load, not silently truncated.
- **HTTP servers only, by default.** A `stdio` server (`transport: "stdio"`,
  a `command` to run) needs `MCP_ALLOW_STDIO=true` — off by default because a
  command string is code execution on the one box holding every tenant's
  data. Only turn it on for a local server you trust.
- **A dead or slow server degrades gracefully, never breaks a turn.** One
  server timing out or erroring is dropped with a logged warning; the others
  and the five native tools keep working.
- `scripts/demo_mcp_server.py` is a zero-credential way to try this end to
  end before pointing at a real vendor — see its docstring.

**No native tool ever moves to MCP.** `check_availability`, `book_job`,
`send_confirmation`, `escalate` and `is_emergency` stay built-in and typed —
MCP is for everything else a business wants to plug in.

## Giving a bot a knowledge base (Phase 9 Part C)

`/admin` → a tenant → the **Knowledge** tab — paste text, upload a file
(`.txt`/`.md`/`.csv`/`.pdf`/`.docx`/`.html`), or crawl a URL. Each becomes a
`knowledge_documents` row that's chunked, embedded, and stored as
`knowledge_chunks` in the background (`app/rag/ingest.py`); the tab polls
every few seconds while a document is `pending`/`indexing` and shows
`ready` or `failed` (with the error) once it settles. A **Search preview**
box on the same tab runs the exact same retrieval `search_knowledge` uses,
so you can sanity-check what the bot would actually find before trusting it
live.

**Nothing about this is in a tenant's JSON file** — unlike everything else
in this document, knowledge documents/chunks live only in Supabase
(`knowledge_source: "supabase"`, the only backend that exists), so
`sync_tenants`/`--export` never touches them and there's no dev-mode
`TENANT_SOURCE=json` equivalent to fall back to.

**Off by default, two switches, not one:**
- `KNOWLEDGE_ENABLED=true` in `.env` gates the admin routes *and* whether
  `search_knowledge` can ever be bound to any tenant, repo-wide.
- `"knowledge": {"enabled": true}` in the tenant's JSON (or the Supabase
  `tenants` row, once `TENANT_SOURCE=supabase`) turns it on for *that*
  tenant specifically — `search_knowledge` is a **conditionally bound**
  native tool, the first one in this codebase; every other native tool is
  bound to every tenant unconditionally.

Needs `GOOGLE_API_KEY` set (embeddings call Gemini's REST API directly,
independent of whatever `LLM_PROVIDER` the reasoning model itself uses) —
see `.env.example`'s knowledge section for the rest of the tuning knobs
(`top_k`, `min_similarity`, `max_upload_bytes`).

**Re-indexing only works for a URL-sourced document.** Pasted text and
uploaded files aren't kept anywhere after their chunks are embedded, so
there's nothing to re-chunk from — re-paste or re-upload instead. A crawled
URL can always be re-fetched from `source_ref`, so its Reindex button
re-crawls, re-chunks, and replaces its chunks in place.

If the bot answers "nothing on file about that" for something you know you
uploaded: check the document's status on the Knowledge tab first (a
`failed` document contributes nothing), then the Search preview box (a
retrieval near, but under, `min_similarity` shows up there as "no results"
just like it would in a live conversation — lowering `min_similarity` a
little is the fix, not re-uploading).

## Creating and removing a bot (Phase 9 Part B)

**Creating one no longer needs a dev box.** `/admin` → "+ New bot" — blank,
from a template (`content/templates/hotel.json` / `clinic.json` /
`trades.json` / `salon.json` / `restaurant.json`), or cloned from an
existing tenant (identity fields — phone numbers, widget key, Vapi
assistant, voice, Cal.com event type — are cleared, everything else
carries over). **A panel-created bot lives in Supabase only — it has no
`content/tenants/<id>.json` counterpart**, since Railway's filesystem is
ephemeral and writing there from the running container would be
misleading, not helpful. Run `python -m scripts.sync_tenants --export`
afterward if you want it committed as a JSON seed/fallback too (see "Which
one is actually 'true'" above for why that matters: without it, a bad edit
that fails Supabase-side validation has nothing to fall back to).

**Removing one is archive-first, purge-separately** — see
`infra/README.md`'s "Removing a bot" section for the full FK order and the
manual SQL fallback. Short version: Archive stops a bot answering on any
channel with nothing deleted (reversible via Restore); Purge is a
separately-confirmed, irreversible deletion of every row the bot has,
including every call and chat transcript, only possible once already
archived.
