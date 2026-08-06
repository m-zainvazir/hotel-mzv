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
| `tenants/<id>.json` → `ui` block | **What the bot may put on screen** — buttons, quick replies, image cards, its own opening message. All on by default; a bot builds these from its AI Prompt with nothing configured. See below. | Turn cards off, restrict which hosts it may link to |
| `tenants/<id>.json` → `links` / `flows` blocks | **Buttons and scripted steps you want pinned down exactly**, rather than left to the bot. A flow renders word for word with no AI involved. | A "Main Menu" button that always says the same thing |
| `tenants/<id>.json` → `channels` block | **Per-channel on/off switches** (Phase 9.1) — see below. | Take a bot off voice while keeping chat live |

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

## Draft, Deploy, and version history (Phase 9.1)

This section is about the `/admin` panel specifically — editing a JSON file
directly under `TENANT_SOURCE=json` (dev/local) is unaffected and still
takes effect on the very next turn, same as always; draft/deploy only
exists on the Supabase-backed admin write path.

`/admin`'s Config and AI Prompt tabs no longer save straight to the running
bot. **Save** now writes a per-tenant **draft** — a single, mutable,
inert row (`tenants.draft_config`) that the runtime never reads. Nothing a
caller hears or sees changes until an operator clicks **Deploy**, which
validates the draft, writes it live (the same path `save_tenant` always
used — consent gate, Supabase fan-out, cache refresh), and records an
immutable `tenant_versions` row. The **Versions** tab is that deploy
history: **Make live** on any past version rolls back (or forward) without
burning a new version number; **Delete** removes any version except
whichever one is currently live.

**A tenant created before this shipped has no versions until its first
Deploy** — there's no synthesized "version 0"; a version row means "this
was actually deployed through this system." A panel-created bot ("+ New
bot") is the one exception: creation deploys immediately, so it starts at
version 1 with nothing to publish.

This is the opposite of the Phase 8 "phantom edit" bug on purpose: that bug
was an edit landing somewhere the running bot didn't read, invisibly.
Here, the gap between "saved" and "live" is the whole point — the banner on
the Config tab ("Draft — not live. N sections changed") is what keeps it
from becoming invisible too.

## Buttons, quick replies and cards need no configuration at all

**Start here, because it's the part most people expect to have to set up
and don't.** Every chat bot can already render buttons, quick replies,
menus and image carousels, with nothing in its config — the tools are bound
on every chat tenant and a shared "How this chat looks" section is added to
every prompt (`ui_rule`, `app/brain/prompts/system.py`). A bot does what its
AI Prompt tells it to:

```
When someone asks about booking, offer two buttons: one that opens
https://example.com/book, and one that says "have someone call me".
```

That's the whole setup. The model composes those buttons itself and the
server renders them.

The `ui` block is the set of switches for turning that **off** again, plus
the guardrail on URLs the bot invents:

```jsonc
"ui": {
  "buttons": true,          // model-composed buttons and quick replies
  "cards": true,            // image carousels
  "opening_turn": true,     // let the bot write its own first message
  "allowed_hosts": [],      // empty = the bot may link anywhere http(s)
  "max_cards": 10
}
```

`allowed_hosts` restricts URLs *the model came up with*, never ones you
typed into `links` below. Leave it empty unless you have a reason: a bot
that can only link where you've pre-approved is also a bot that can't link
to something useful it found. Non-`http(s)` URLs (`javascript:`, `data:`)
are always rejected regardless.

`opening_turn` costs one AI request per visitor who opens the widget,
including those who never type — it's what lets the opening menu come from
the prompt. It's ignored when you've configured a `menu_flow`, since that
renders instantly and for free.

## Pinning a button down exactly (`links`)

Everything above is the bot improvising. `links` is for when you don't want
it to: a URL that must always be correct, a label that must always read the
same. It's one catalog feeding **four** places — the greeting menu, a
flow's buttons, a card's buttons, and `offer_actions` — so you declare a
button once and reference it by slug everywhere.

```jsonc
"links": [
  { "slug": "book-online", "label": "Book online", "type": "link",
    "url": "https://example-hotel.com/book", "description": "our booking page" },
  { "slug": "talk-to-someone", "label": "Talk to someone", "type": "handoff",
    "description": "reach the front desk right now" },
  { "slug": "main-menu", "label": "🏠 Main Menu", "type": "flow", "flow": "main-menu" },
  { "slug": "call-me-back", "label": "📞 Have our team contact you", "type": "reply",
    "value": "please have someone contact me" }
]
```

| `type` | What clicking it does |
|---|---|
| `link` | Opens `url`. Required for this type. |
| `flow` | Jumps to the `flow` node below — **deterministic, no AI request at all**. |
| `reply` | Sends `value` (or the label) as if the visitor typed it; the AI answers. |
| `handoff` | Same as `reply`, but the phrase is what makes the bot call `escalate`. |

A catalog button is exempt from `allowed_hosts` — you wrote it, so the
guardrail meant for the model doesn't apply. **Chat-only** either way: a
voice caller can't click a button, so none of this is bound on that
channel.

## Scripted flows and a persistent menu (Phase 9.2)

A flow is one step: fixed wording plus buttons. Clicking a `flow` button
shows it **exactly as written, with no AI involved** — which is what makes
a `Main Menu` button behave identically every single time, instead of
"usually".

```jsonc
"flows": [
  { "id": "main-menu",
    "say": "What can I help you with today?",
    "buttons": ["book-appointment", "find-location", "careers"],
    "description": "the top-level menu — start this when someone is unsure" },
  { "id": "locations",
    "say": "You can view all our locations below in 'Browse Locations'.",
    "buttons": ["browse-locations", "book-appointment", "main-menu"],
    "description": "someone wants to find a clinic or an address" }
],
"chat": { "menu_flow": "main-menu" }
```

- `say` is shown **word for word**. It is not a prompt; no model sees it first.
- `description` is the opposite — it's what the AI reads to decide whether to
  jump here on its own when someone types free text (via the `start_flow`
  tool) rather than clicking.
- `chat.menu_flow` names the flow whose buttons appear under the greeting,
  before the visitor types anything. Point your `Main Menu` button at the
  same flow and the two can never drift apart. Leave it unset and the bot
  keeps showing service chips exactly as before.
- **A flow can't ask for or store anything.** For a name, a zip code or a
  phone number, add a `reply` button and let the AI take over — it's far
  better at open-ended capture than a fixed form. Branching is what buttons
  are.
- Every cross-reference is checked when you save: a button pointing at a
  missing flow, a flow listing a missing button slug, or a `menu_flow` that
  doesn't exist all fail validation with the exact field named, rather than
  rendering a dead button.

`content/templates/clinic.json` ships a complete worked example (menu,
booking flow, locations flow) — the fastest way to see the shape.

## Card carousels

An image, a title, a subtitle and its own buttons, in a swipeable row — for
products, rooms, locations, events, anything with a picture. **On by
default**; the bot builds them from its prompt, e.g.:

```
When someone asks for room options, show them as image cards with the
room name, the nightly rate, and a "Book this room" button.
```

Card data is model-supplied by necessity — a scraped product's image and
link are found mid-conversation and can't come from a catalog — so
`ui.allowed_hosts` and the scheme check are what stand between it and the
browser. A card *button* can still name a catalog slug, which skips the
allowlist as usual.

Turn the whole thing off with `"ui": {"cards": false}`; the bot is then told
so in its prompt rather than being left to call a tool that refuses.

## Pasted prompts and the `${ui_rule}` / `${links}` / `${flows}` sections

A prompt pasted in from another platform contains none of `${ui_rule}`,
`${links}` or `${flows}` — so on its own the AI would never learn it can
render anything at all, let alone which buttons you configured. By default
the missing sections are appended automatically at the end. Put a
placeholder in yourself wherever you'd rather it appear and the automatic
copy for that one stops.

```jsonc
"prompt_augmentation": "auto_append"   // or "placeholder_only" to never touch your text
```

The AI Prompt tab shows a banner (with this setting inline) whenever a bot
has buttons its prompt doesn't mention.

## Turning a channel off for one bot (Phase 9.1)

```jsonc
"channels": {
  "chat": { "enabled": true },
  "voice": { "enabled": true }
}
```

Both default `true`, so no existing tenant's behaviour changes. Disabling
one 404s that door for this bot only — `POST /chat/session` (and therefore
the widget and any Test Agent link in chat mode) for `chat: false`,
`POST /chat/completions` (therefore every phone call and web call) for
`voice: false`. The other channel is never affected either way.

## The Test Agent link (Phase 9.1)

`/admin` → a tenant → the **Test Agent** button in the header (present on
every tab) mints a signed, shareable `/test/{token}` link and opens it in a
new tab — a full page embedding the real widget, auto-opened, with no
widget key needed (so a tenant with an empty `widget_keys[]` is still
testable this way). It always reflects the **live** config, never an
undeployed draft, and expires on its own (`TEST_LINK_TTL_SECONDS`, default
24h) — nothing to revoke by hand. Needs `PUBLIC_BASE_URL` set, same as
voice provisioning. Greyed out when `channels.chat.enabled` is `false` for
that tenant.
