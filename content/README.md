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

## Syncing a tenant file to Supabase (Phase 4)

The brain still reads tenant config from these JSON files, not Supabase —
that hasn't changed. But Supabase's `tenants`/`services` tables exist
alongside them (for onboarding + the eventual read-path flip), and they only
update when you tell them to:

```
python -m scripts.sync_tenants --tenant hotel-mzv    # one tenant
python -m scripts.sync_tenants                        # every tenant
```

Safe to run any time — it's an upsert, and skipping it costs you nothing
today (nothing reads those tables yet). Needs `SUPABASE_URL` +
`SUPABASE_SECRET_KEY` in `.env`.

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
