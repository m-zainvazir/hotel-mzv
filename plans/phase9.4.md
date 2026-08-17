# Phase 9.4 — Cal.com owns availability; Asia/Karachi by default

> Does **not** touch Phase 9.3 (the voice tester, `plans/phase9.3.md`) — that stays queued
> behind a Deepgram key.

---

## Context

Two unrelated-looking complaints, one root cause: **a bot's opening hours are configured in two
places that disagree, and only one of them is real.**

`hotel-mzv` is on `booking.provider: "calcom"`. Cal.com's own event-type schedule is what actually
decides which slots `check_availability` returns. But the tenant config still carries a full weekly
`hours` grid plus `slot_granularity_minutes` and `lead_time_hours`, and the admin panel still
renders all of them as editable fields. Editing them changes **nothing** for that bot — the panel
shows working controls that are wired to nothing. CLAUDE.md already documents this as a gotcha; this
phase makes the product match the documentation instead of relying on the operator remembering it.

Separately, every bot and template is anchored to a US timezone (`America/New_York`,
`America/Chicago`) and the code default is `UTC` — none of which is where this business operates.
Timezone is doubly load-bearing here: it governs which slots get offered *and*, on the Cal.com side,
two separate settings (the schedule's timezone and the account profile's timezone) that produce a
calendar showing a different clock — and sometimes a different day — when they drift apart.

**Outcome:** when a bot is connected to Cal.com, Cal.com is the only place its availability lives,
and the panel says so instead of pretending otherwise. When it isn't connected, the manual grid
stays and keeps working. Everything defaults to Asia/Karachi, and changing a bot's timezone is a
picker, not a field you have to spell correctly.

### Decisions taken (from clarification)

| | |
|---|---|
| Manual hours | **Conditional, not deleted.** Connected to Cal.com → hours are read from Cal.com and the manual fields disappear. Not connected → the manual grid stays editable and is what the bot uses. |
| New bots | New Bot form gains a **Cal.com event type ID** field. Filled in → `provider: "mcp_calcom"`. Blank → `stub`, with a "not connected" banner. OAuth grant stays the existing `scripts/authorize_calcom.py` command, surfaced by name in the panel. |
| `hotel-mzv` | **Switch it to `mcp_calcom` too**, with a live re-verification of a real availability query and a real booking. |
| Timezone | Default `Asia/Karachi` for new bots; every existing bot changed to it; the Cal.com account's **schedule** timezone and **profile** timezone both changed to it. |

### What this deliberately does *not* do

- **No migration.** `hours` and every booking knob live inside the `config` JSONB (outside
  `_TENANT_COLUMNS`, `app/tenancy/sync.py:31`). `timezone` *is* a real column with a SQL default of
  `'UTC'` (`0001_schema.sql:32`), but `_tenant_row` always writes it explicitly, so that default has
  never once been exercised — changing it would be a migration that alters nothing observable.
- **No new model for hours.** `DayHours` / `hours_summary()` / `is_open_at()` all stay exactly as
  they are; they simply stop being the source of truth for Cal.com-connected bots.
- **`horizon_days` and `max_slots_returned` stay editable.** They are not availability — Cal.com has
  no opinion on how far ahead we query or how many options the bot reads out. They get relabelled so
  that's obvious, not removed.
- **No panel-side OAuth.** A "Connect Cal.com" button in `/admin` needs a server callback route and
  its own live verification pass; the terminal command already works and is a one-time step per bot.

---

## Architecture — three decisions worth stating first

### D1. "Connected to Cal.com" is a computed status, not a config flag

Adding a `calcom_connected: bool` to `TenantConfig` would be a fourth place for this to drift. It is
derived, on demand, from things that are already true or not true:

```
connected  ==  provider in ("calcom", "mcp_calcom")
           and an event type is resolvable (booking.event_type_id or every service overriding it)
           and a credential exists   ─ mcp_calcom → an OAuth grant in Vault for THIS tenant
                                     └ calcom     → a per-tenant or shared API key
```

Exposed by one read-only route, `GET /admin/api/tenants/{id}/calcom`, returning
`{provider, connected, event_type_id, timezone, schedule, reason}`. `reason` is the human sentence
the panel shows when `connected` is false ("no OAuth grant — run `python -m scripts.authorize_calcom
--tenant hotel-mzv`"), so the UI never has to reconstruct why.

The grant check is a cheap Vault read via a new public `has_grant(tenant_id)` in `app/mcp/oauth.py`
wrapping the existing `_load_credentials` — **not** `access_token_for`, which would perform a real
token refresh against Cal.com just to render a badge.

### D2. Business hours become a provider capability, resolved async and cached

`BookingProvider` (`app/tools/booking/base.py`) gains one optional method:

```python
async def business_hours(self, tenant: TenantConfig) -> str | None:
    """Human-readable weekly availability, or None if this provider has no
    schedule of its own. Never raises — the prompt must render regardless."""
    return None
```

- `McpBookingProvider` → the Cal.com MCP schedule tool if Step 0's spike finds one; otherwise
  derived from a `get_availability` sweep (a tool whose shape is already live-verified).
- `CalcomBookingProvider` → the same, over REST.
- `StubBookingProvider` → `tenant.hours_summary()`, i.e. the manual grid. This is the branch that
  keeps a not-connected bot behaving exactly as it does today.

Cached per tenant with a short TTL in a new `app/tools/booking/schedule.py`, module-level for the
same reason `McpBookingProvider._session_cache` is (`get_booking_provider()` builds a fresh provider
on every call, so caching on `self` would cache nothing). Any failure returns `None` and logs — a
Cal.com hiccup must degrade the prompt's hours line, never break the turn.

`reason` (`app/brain/nodes/reason.py`) is already `async` and already calls `render_system_prompt`
synchronously, so it awaits the cached lookup and passes the result in as a new
`business_hours: str | None = None` parameter. `system.py` uses it when present, falls back to
`tenant.hours_summary()` when not, and when **neither** yields anything renders a line telling the
model availability is live and to call `check_availability` rather than quoting hours — which is
strictly better than today's failure mode, where an empty grid renders "Mon closed, Tue closed, …"
and the bot confidently tells callers it never opens.

### D3. The panel hides what doesn't work, rather than labelling it

Config.tsx already carries a note reading *"Once provider is calcom, Cal.com's own event-type
schedule governs availability — hours/lead-time/granularity below become prompt copy only."* That
note has been there and has not stopped the confusion, because a disabled-looking sentence above a
fully working set of inputs loses. When `connected` is true the Hours grid and the two dead knobs
are **not rendered at all**; in their place is Cal.com's actual schedule, read-only, with a
"Managed in Cal.com" badge and a link out to the event type.

---

## Steps

### Step 0 — Spike: what does the Cal.com MCP server expose? *(read-only, do this first)*

Open a session against `https://mcp.cal.com` for a tenant with a grant and call
`session.list_tools()` — the plumbing already exists in `app/tools/booking/mcp_calcom.py`
(`_call_tool` uses the same `ClientSession`). Record, in this doc, the full tool list and
whether any of them returns an availability *schedule* (as opposed to bookable slots), plus its
argument shape.

This decides one thing only: whether `business_hours` reads a schedule directly or derives the
weekly pattern from a `get_availability` sweep. **Both paths ship the same interface**, so this
cannot block the rest of the phase — it only decides which branch is the primary. Follow Phase 9
Part A's precedent and write the recorded exchange into the module docstring, since Cal.com's docs
name tools without naming their parameters.

### Step 1 — Timezone defaults

- `app/tenancy/models.py:457` — `timezone: str = "UTC"` → `"Asia/Karachi"`.
- `content/templates/*.json` (all five) and `content/tenants/*.json` (both) → `"Asia/Karachi"`.
- Leave `0001_schema.sql`'s column default alone (never exercised — `_tenant_row` always writes the
  value explicitly).

### Step 2 — A timezone picker instead of a spelling test

`admin/src/views/Config.tsx` (~line 323), replacing the `TextField`:

- A `<select>` populated from `Intl.supportedValuesOf("timeZone")` — the full IANA list, from the
  browser, zero bundle cost — with `Asia/Karachi` pinned to the top and a short curated fallback
  array for any browser without it.
- The zone's **current local time** rendered beside it and updating on change, so picking the right
  one is a glance rather than a guess.
- For a Cal.com-connected bot, a warning when this value differs from the schedule timezone returned
  by D1's route — the exact drift that produced the "Karachi account displayed a New-York 8pm
  booking as next-day 5am" bug already in CLAUDE.md.

The existing `_real_timezone` validator (`models.py:532`) needs no change; it already maps an
unknown zone to a clean 422 rather than a 500.

### Step 3 — Business hours from the provider

- `app/tools/booking/base.py` — the `business_hours` method above, defaulting to `None`.
- `app/tools/booking/schedule.py` (new) — the TTL cache + `async def business_hours_for(tenant)`,
  never raising.
- `app/tools/booking/mcp_calcom.py`, `calcom.py`, `stub.py` — one implementation each.
- `app/brain/nodes/reason.py` — await it, pass it through.
- `app/brain/prompts/system.py` — accept `business_hours: str | None = None`; use it, fall back to
  `tenant.hours_summary()`, and emit the "availability is live, call `check_availability`" line when
  both are empty.
- `content/system-prompt.md` — the `Business hours: ${business_hours}` line stays as-is; only what
  fills it changes.

### Step 4 — The conditional Hours UI

- `app/channels/admin.py` — `GET /admin/api/tenants/{id}/calcom` (D1). Read-only, behind the same
  `require_tenant_access` every other tenant route uses.
- `app/mcp/oauth.py` — public `has_grant(tenant_id)`.
- `admin/src/api.ts` — `getCalcomStatus(tenantId)` returning a `CalcomStatus` interface. **Type this
  against what the route actually returns** — an inaccurate annotation here silently disables
  `tsc --noEmit`, which is the only wire-contract guard the panel has (the `createTenant` /
  `TenantDetail` bug is the precedent).
- `admin/src/views/Config.tsx` — `HoursSection` takes the status: connected → read-only Cal.com
  schedule + "Managed in Cal.com" badge; not connected → today's editable grid + a "not connected —
  these times are simulated" notice. `BookingSection` hides `slot_granularity_minutes` /
  `lead_time_hours` when connected, relabels `horizon_days` / `max_slots_returned` as conversation
  settings, and shows the `reason` sentence with the `authorize_calcom` command when a Cal.com
  provider is selected but not connected.

### Step 5 — New Bot asks for the event type

`admin/src/views/NewTenant.tsx` + the create payload in `app/channels/admin.py`:

- One optional **"Cal.com event type ID"** field with a one-line explainer.
- Filled → `booking.provider = "mcp_calcom"` and `booking.event_type_id = <id>`; templates keep
  shipping `"stub"` so they stay valid on their own (`_calcom_tenants_declare_event_types` fails a
  Cal.com provider with no event type at config load — correctly, and that guard stays).
- Blank → `stub`, and the Config tab's banner explains what to do when they're ready.
- After creating a Cal.com bot, the success screen names the grant command for that exact tenant id.

### Step 6 — Move `hotel-mzv` onto `mcp_calcom` *(live)*

`python -m scripts.authorize_calcom --tenant hotel-mzv`, flip `booking.provider`, then re-verify
against the real account: a real `check_availability` returning real slots, and a real `book_job`
end to end. Keep the REST value recorded so reverting is a one-word edit if anything misbehaves.

### Step 7 — Cal.com account timezone *(live, outward-facing)*

Set **both** timezones to `Asia/Karachi`: the availability schedule's, and the account profile's.
Prefer `PATCH` over the Cal.com v2 API (verifiable, repeatable) with the dashboard as the fallback
if either endpoint disagrees. Confirm the exact calls with the user before running them, since this
edits a real account rather than this repo.

### Step 8 — Roll the timezone change to the live bots

Production runs `TENANT_SOURCE=supabase`, so live config is edited **through `/admin`**, not by
committing JSON — `scripts/sync_tenants.py` deliberately refuses to run against a Supabase read path
without `--force` precisely so it can't stomp panel edits. Change `hotel-mzv`,
`northside-plumbing`, and the three scratch bots (`playmouth1`, `playmouth2`, `test-clinic`) if
they're still around; the repo JSONs from Step 1 are the seed/fallback copies.

---

## Testing

Extend `tests/test_tenant_config.py`, `tests/test_system_prompt.py`, `tests/test_native_tools.py`,
`tests/test_admin_write.py`; new `tests/test_booking_schedule.py`.

The ones that carry weight beyond the obvious:

- **A Cal.com-connected tenant's prompt quotes Cal.com's hours, not the local grid** — set the two
  to deliberately different values and assert which one reaches the prompt. This is the whole phase
  in one test.
- **A stub tenant is byte-identical to today.** The guard that makes this safe to ship.
- **A provider that raises degrades to the local grid**, and the turn still completes.
- **Neither source available → the "call check_availability" line renders**, and never
  "Mon closed, Tue closed, …", which is the specific wrong answer today's empty grid produces.
- **`has_grant` is false for a tenant with no grant and never performs a token exchange** (assert no
  HTTP call) — otherwise rendering the panel would hammer Cal.com's token endpoint.
- **The `connected` computation** across the matrix: stub / calcom-with-key / calcom-no-key /
  mcp-with-grant / mcp-no-grant / provider set but no event type.
- **Timezone default is `Asia/Karachi`** for a config built from nothing, and every template parses.

## Verification (live)

Offline first: `pytest`, `ruff check .`, `ruff format .`, `npm --prefix admin run build` with the
`dist/` + `.buildhash` committed.

Then, against the real project:

1. `/admin` → `hotel-mzv` → Config: the Hours grid is **gone**, replaced by Cal.com's real schedule
   with the Managed badge; slot-granularity and lead-time are gone; horizon/max-slots remain.
2. A real chat turn asking "what time do you open?" quotes hours matching what Cal.com shows.
3. Timezone picker: switch to another zone, see the local-time preview follow, save, and confirm the
   *next* turn's offered slots move accordingly. Switch back to Asia/Karachi.
4. Step 6's booking, checked in the Cal.com dashboard — right day, right clock, now that both
   timezones agree.
5. A new bot with the event type field **blank**: Hours grid present and editable, banner naming the
   grant command; slots still offered from the manual grid.
6. The same bot with an event type filled in, then `authorize_calcom` run: the banner clears and the
   grid is replaced by Cal.com's schedule without touching anything else.

## Docs to update when it's done

- `plans/phase9.4.md` — this file, plus Step 0's recorded tool list and a live-verification record
- `CLAUDE.md` — Current state; and **rewrite** the "Once a tenant is on `booking.provider: calcom`,
  Cal.com owns availability" gotcha, which currently documents the trap this phase removes
- `content/README.md` — hours are Cal.com's for a connected bot; the manual grid is the stub path
- `admin/README.md` — the connected/not-connected states of the Config tab

---

## Implementation record

### Steps 1–5 — done, offline-tested and green (1034 tests, ruff clean)

- **Step 1.** `TenantConfig.timezone` defaults to `Asia/Karachi`; all five templates and both
  tenant JSONs updated. Two tests were asserting the old US zones as though they were fixed facts
  (`test_tenancy.py::test_tenant_profile_fields`, `test_brain_end_to_end.py::
  test_tenant_config_reaches_the_system_prompt`) — rewritten to assert against the tenant's own
  declared value, the same fix Phase 7 applied to the `allow_warm_transfer` tests.
- **Step 2.** `TimezoneField` (`admin/src/views/Config.tsx`) — a `<select>` over
  `Intl.supportedValuesOf("timeZone")` with a curated fallback, showing the current local time in
  the selected zone. A zone the browser doesn't list is preserved rather than silently swapped.
- **Step 3.** `BookingProvider.availability_schedule` (non-abstract, returns `None`) plus
  `AvailabilitySchedule` / `ScheduleWindow` in `app/tools/booking/base.py`;
  `app/tools/booking/schedule.py` caches per tenant, fingerprinted on
  provider/event-type/timezone/grid so a config edit invalidates immediately, and never raises.
  `reason` awaits it and passes `business_hours=` into `render_system_prompt`.
- **Step 4.** `GET /admin/api/tenants/{id}/calcom` + `has_grant()` in `app/mcp/oauth.py`; the
  Config tab's Hours section now renders Cal.com's real schedule read-only when connected, and the
  editable grid otherwise. Slot granularity / lead time are hidden when a calendar owns them.
- **Step 5.** New Bot gained an optional **Cal.com event type ID** (filled → `mcp_calcom`) and a
  timezone picker; the form names the `authorize_calcom` command for that exact tenant id.

**Step 0, done properly once a working grant existed.** `session.list_tools()` against
`https://mcp.cal.com/mcp` returns **59 tools**, far more than the four this codebase knew about,
including a full schedule family: `get_default_schedule`, `get_schedules`, `get_schedule`,
`create_schedule`, `update_schedule`, `delete_schedule` — plus `get_busy_times`,
`get_connected_calendars`, `confirm_booking`, `mark_booking_absent`, and a large org/team/routing
surface.

This **corrects a claim made earlier in this phase from the docs rather than the server**: the
first version of `McpBookingProvider.availability_schedule` said Cal.com's MCP server "exposes no
schedule tool" and delegated to REST for that reason. It does expose one. `get_default_schedule`
takes no arguments and returns byte-identical JSON to the REST endpoint, so
`_schedule_from_calcom` maps both and the provider now reads it natively. That closes the caveat
about an `mcp_calcom` tenant needing an API key just to quote its hours.

Still rejected, and worth restating: inferring hours from a `get_availability` sweep. A
fully-booked Tuesday comes back empty and the bot would tell callers it's closed on Tuesdays.

`GET /v2/schedules` (`cal-api-version: 2024-06-11`) remains the REST provider's source, same
shape: `{name, timeZone, availability: [{days, startTime, endTime}], isDefault, overrides}`.

### Three things worth recording

- **Cal.com's Cloudflare 403s a bare `Python-urllib` User-Agent** (`error code 1010`). httpx's
  default UA is fine, which is why the app itself has never hit this — but any one-off script
  against `api.cal.com` needs a browser-like UA or it looks exactly like an auth failure.
- **`business_hours_for` cannot be imported at module scope in `reason.py`.** `app/tools/__init__.py`
  pulls in the whole tool registry, which reaches back through `app.flows` → `app.brain` →
  `app.brain.nodes.reason`. The existing `from app.tools.registry import native_tools_for` survives
  only because it resolves after that cycle unwinds; a second, alphabetically-earlier tools import
  does not. It's a function-level import with a comment saying so.
- **A Cal.com tenant never falls back to its stale config grid.** `_config_hours`
  (`app/brain/prompts/system.py`) returns the grid only for `provider == "stub"`. Once a calendar
  owns availability the grid is whatever someone typed before that, nobody maintains it, and the
  panel now hides it — reciting it would be confidently quoting months-old hours. With no source at
  all the prompt says so and tells the model to quote `check_availability` instead, which replaces
  the old failure mode where an empty grid rendered as "Mon closed, Tue closed, …" and the bot told
  callers it never opens.

### Step 7 — done, live

Both Cal.com timezones moved to `Asia/Karachi` and were confirmed in the responses:
`PATCH /v2/schedules/1678330` (`cal-api-version: 2024-06-11`, body `{"timeZone": "Asia/Karachi"}`)
and `PATCH /v2/me` (`{"timeZone": "Asia/Karachi"}`). The schedule keeps its `07:00–22:00` window —
same wall clock, now Karachi's. Reverting is the same two calls with the old value.

Note these are genuinely two settings, not one: the schedule governs *availability*, the profile
governs how the dashboard *displays* what was booked. `GET /v2/me` also confirms
`defaultScheduleId: 1678330`, which is what makes reading the default schedule the right choice in
`CalcomBookingProvider.availability_schedule`.

### Live verification (2026-08-17)

Against the real Supabase project and the real Cal.com account, through `/chat/session` + `/chat`:

- **"What time do you open and close?"** → *"We are open daily from 7:00 AM to 10:00 PM."* That
  sentence exists because the prompt was filled from Cal.com's schedule; the tenant's own config
  grid is not consulted for this bot at all.
- **"Any spa treatment slots late tomorrow night?"** → real Cal.com slots at
  `2026-08-18T19:00:00+05:00`, `20:00+05:00`, `21:00+05:00`. `+05:00` is Pakistan Standard Time, so
  the timezone change reached the booking path and not just the spoken copy.
- `GET /admin/api/tenants/hotel-mzv/calcom` → `connected: true`, the real schedule, and
  `timezone_matches: true` (it was correctly `false`-equivalent before Step 7, when the tenant had
  moved and Cal.com hadn't).

### Step 8 — partly live

Against the real project (`TENANT_SOURCE=supabase`), through the admin API:

| bot | was | now |
|---|---|---|
| `playmouth1` | `UTC` | **`Asia/Karachi`** (deployed) |
| `playmouth2` | `UTC` | **`Asia/Karachi`** (deployed) |
| `test-clinic` | `America/New_York` | **`Asia/Karachi`** (deployed) |
| `northside-plumbing` | `America/Chicago` | staged in its existing draft, **not deployed** |
| `hotel-mzv` | `America/New_York` | **`Asia/Karachi`** (deployed, after Step 7) |

`northside-plumbing` already had an unpublished draft (greeting with a doubled `?`) from an earlier
session. Deploying would have published that too, so the timezone joined the same draft and the
deploy is the operator's call.

`hotel-mzv` was deliberately done last: changing the tenant timezone while Cal.com's schedule still
said `America/New_York` would have had it computing Karachi wall-clock times against a New-York
schedule, on a bot that is genuinely booking. Step 7 first, then this.

**A near-miss worth remembering:** reading the admin API with `curl … | python -c` made every
em-dash look like `â€”`, i.e. exactly like live data corruption. It was the *read* — Python decodes
stdin as cp1252 on this box. `PYTHONIOENCODING=utf-8` plus `urllib` (which decodes JSON as utf-8 by
spec) shows the data is clean. Same trap CLAUDE.md already records for *writing* config from a
Windows shell; it applies to reading it back too.

### Step 6 attempt — blocked on a dead Cal.com grant, and it found a real bug

The grant was authorized interactively and `has_grant("hotel-mzv")` returned True (and correctly
False for `northside-plumbing` — the Vault scoping holds). **The provider was not flipped**, because
testing the MCP path in memory first — the whole point of testing before writing config — failed:

```
McpBookingProvider.check_availability -> BookingError: could not reach the calendar
  caused by: httpx 401 Unauthorized for https://mcp.cal.com/mcp
```

What the diagnosis established, in order:

1. `access_token_for("hotel-mzv")` **succeeds** — the token endpoint returns 200 with a real
   `expires_in: 3600` and `token_type: bearer`.
2. That token is rejected as `invalid_token` ("Invalid or expired access token") by
   `mcp.cal.com/mcp` — reproduced with a hand-rolled `initialize` POST, so it isn't the MCP client
   library's doing.
3. The same token is **also** rejected by `api.cal.com/v2/me` (`UnauthorizedException`). So the
   token is not merely unaccepted by one resource server; it is dead everywhere.
4. Adding RFC 8707 `resource=https://mcp.cal.com` to the refresh request changes nothing (the token
   endpoint still returns 200, the resource server still 401s), so audience binding is not the gap.

**Conclusion:** the authorization server is happily minting tokens against a grant it no longer
honours. Not recoverable from this side — it needs a fresh authorization.

**The bug found along the way, which is a plausible cause and is fixed:**
`set_tenant_secret` did not invalidate `get_tenant_secret`'s read cache (300s TTL). Cal.com
**rotates the refresh token on every single refresh** and invalidates the previous one immediately;
`access_token_for` persists the rotated value, but any read within the next five minutes in that
process kept returning the spent one. Presenting a spent refresh token is exactly the pattern
RFC 6819 §5.2.2.3 tells an authorization server to treat as replay — and revoking the grant while
still answering refresh calls is a defensible way to respond to it. Reproduced the shadowing
directly (probe B above hit `invalid_grant` for precisely this reason), fixed in
`app/tenancy/secrets.py`, and guarded by
`test_tenant_secrets.py::test_a_write_invalidates_the_read_cache` — verified to fail without the
fix (`assert 'old_refresh_token' == 'new_refresh_token'`).

**`scripts/authorize_calcom.py` now verifies the grant before reporting success** (`_verify_grant`):
it clears the secret cache, goes through the *headless* refresh path rather than reusing the code
exchange's own access token, and opens a real `initialize` against the MCP server. Storing a token
and printing ✓ was one round trip short of knowing anything — this exact failure was invisible until
a booking attempt, where it surfaces as "could not reach the calendar", which points at the network
instead of at the grant.

### Step 6 — done and live-verified

Re-authorized, and the fresh grant works: `initialize` against `mcp.cal.com` returns 200.
`hotel-mzv` is now `booking.provider: "mcp_calcom"`, deployed, and proven end to end:

- **Opening hours over MCP** — "What time do you open?" → *"open daily from 7:00 AM to 10:00 PM"*,
  now sourced from `get_default_schedule` rather than REST.
- **Availability** — real slots at `+05:00`; cold 0.84s, warm 0.63s, both inside the §13 budget and
  effectively tied with the REST provider.
- **A real booking**, driven through `/chat` and pulled back from Cal.com's own `/v2/bookings`:
  `uid hxHbVJdjuQurze`, `2026-08-18T13:00:00Z` = **18:00 Asia/Karachi**, exactly the 6pm slot
  offered, 60 minutes, event type `6446177`, attendee `caller-923001234567@example.com`, metadata
  `{tenant_id: hotel-mzv, service_slug: spa-treatment, channel: chat}` — identical in shape to the
  REST-provider bookings sitting beside it. Its attendee timezone reads `Asia/Karachi`; every
  earlier booking on this calendar reads `America/New_York`, which is the Step 7 change visible on
  the calendar itself.

That test booking is still on the calendar (2026-08-18 18:00) alongside earlier ones from previous
sessions — cancel them in Cal.com whenever convenient.

### Two bugs the switch exposed, both fixed

**1. An import cycle that only a script could hit.** `scripts/authorize_calcom.py`'s new
verification step crashed with `ImportError: cannot import name 'native_tools_for' from partially
initialized module 'app.tools.registry'`. Two cycles existed:

```
app/tools/__init__.py -> registry -> action_tools -> app.flows -> app.flows.render
  -> app.brain.events -> app/brain/__init__.py -> app.brain.graph
  -> app.brain.nodes.reason -> app.tools.registry            (partial)
... -> app.brain.runner -> app.flows.render                  (partial)
```

The root was `app/brain/__init__.py` eagerly importing the graph, so touching *any* leaf of
`app.brain` built the whole thing — `app/flows/render.py` wants one dataclass out of
`app.brain.events` and had no business compiling a graph to get it. Nothing imports
`from app.brain import ...` at all, so those re-exports are now lazy (PEP 562 `__getattr__`),
which breaks both cycles at the source. An initial attempt scattered function-local imports across
four modules in `app.brain`; those were reverted once the root fix landed, because they'd have left
comments claiming a cycle that no longer exists.

Invisible for months because the app, every `app.main`-importing script, and the whole test suite
resolve the package graph early enough that the loop is already closed.
`tests/test_import_cycles.py` runs cold imports in a **subprocess** for exactly that reason —
module state is process-global, so any earlier import in the same interpreter hides it.

**2. A shape mismatch that failed silently.** After the provider flip, the admin panel reported
"Connected, but Cal.com didn't return a schedule" — with nothing in the logs. REST returns a
**list** of schedules; MCP's `get_default_schedule` returns a **single dict**, its envelope already
stripped by `_call_tool`'s `_unwrap_envelope`. `_schedule_from_calcom` understood only the list, so
it returned `None` — no exception, no log line, just a missing hours line. It now normalises all
four wrappers (bare/enveloped × list/dict), covered by `TestScheduleShapes`.

Worth noting the failure mode: this layer is deliberately built to degrade quietly so a calendar
outage can't kill a turn, and the cost of that is that a mapping bug is also quiet. Only a live
check surfaces it.
