# Admin dashboard (Phase 8)

Analytics + per-tenant config editing at `/admin`, served same-origin from
the one FastAPI app (`app/main.py`'s guarded `StaticFiles` mount — see
`app/channels/admin.py` for the API it talks to).

Unlike `widget/`, this is **not** an embed contract — nothing pastes a
`<script>` tag pointing at it, so it builds as a plain Vite SPA (hashed
asset filenames, `index.html` entry) rather than a single dependency-free
IIFE. It still mirrors `widget/`'s conventions closely: Preact + TypeScript,
a committed `dist/` guarded by a `.buildhash` (see `scripts/buildhash.mjs`
and `tests/test_admin_bundle.py`), no chart library (bars/lines over ≤90
daily points are ~80 lines of inline SVG, see `src/charts/BarChart.tsx`), and
no routing library (a ~30-line hash router in `src/router.ts`).

## Build

```
npm --prefix admin install
npm --prefix admin run build     # writes dist/ + dist/.buildhash — commit both
```

Re-run after any `admin/src` edit; `tests/test_admin_bundle.py` fails if you
forget (it recomputes the hash in Python — no Node needed in CI).

**Windows Application Control gotcha (this dev box), same class as
`uuid_utils`/`ruff` in `CLAUDE.md`:** a fresh `npm install` resolves the
newest `4.x` rollup, whose native `@rollup/rollup-win32-x64-msvc` binary is a
file this box's Application Control policy hasn't trusted yet — the build
fails with `Cannot find module @rollup/rollup-win32-x64-msvc` wrapping an
`ERR_DLOPEN_FAILED` / *"An Application Control policy has blocked this
file"*. `widget/`'s committed lockfile happens to pin the older `rollup@4.62.2`
build, which this box has already trusted from earlier work — `package.json`
pins `vite` to the exact same `5.4.21` and adds `"overrides": {"rollup":
"4.62.2"}` for the same reason. If a future bump needs a newer rollup, expect
to hit this again; there's no code fix, only getting the new binary trusted
(or pinning back down) on whatever box builds it.

## Auth

There is no login system of its own yet — operators paste the
`ADMIN_AUTH_TOKEN` bearer directly into the login screen, stored in
`sessionStorage` (cleared on browser close, not `localStorage`, given the
size of this token's blast radius — see `app/channels/admin_auth.py`). Real
per-tenant login is `plans/phase10.md` item 14; the UI already branches on
`GET /admin/api/session`'s `kind` field (`"operator"` vs `"tenant"`) rather
than assuming operator, so that flip needs no UI rewrite — only a login
screen swap and a second `require_admin` branch server-side.

## Layout

```
src/
  main.tsx        entry — mounts <App/> into #root
  App.tsx         login gate, sidebar nav, route switch
  router.ts       hash router (#/  and  #/tenants/:id/:tab)
  api.ts          the /admin/api/* client
  styles.css      global page styles (not shadow-DOM scoped — this is a
                  normal top-level page, not embedded into a third-party site)
  charts/
    BarChart.tsx  inline SVG, no dependency
  views/
    Overview.tsx           tenant list + headline metrics
    NewTenant.tsx           "+ New bot" — blank / template / clone (Phase 9 Part B)
    TenantView.tsx         per-tenant tab shell
    Metrics.tsx            tiles + daily charts
    Config.tsx             the tenant config editor — see below
    SystemPrompt.tsx        the "AI Prompt" tab — see below
    Calls.tsx               calls list + one-at-a-time transcript drill-in
    Chats.tsx               chat sessions list + message drill-in
    JobsEscalations.tsx     bookings + escalations lists
```

## The config editor (`views/Config.tsx`)

Whole-document PUT with a shallow top-level merge, matching
`app/channels/admin.py::put_tenant`'s own semantics exactly: editing one
scalar field (the greeting, say) sends just that key, but editing anything
inside a nested section (`voice`, `booking`, `hours`, `services`, ...) always
submits that section's *complete* current state — never a sparse delta
within it, since the server's merge is shallow at the top level only.

Operator-only fields (`app/tenancy/admin.py::OPERATOR_ONLY_PATHS`) render as
disabled inputs when the signed-in principal's `kind` isn't `"operator"` —
today that's always true (no tenant-login branch exists yet), but the UI is
already principal-aware so the day it lands, nothing here needs to change.

A 422 response's `detail` is a list of `{loc, msg, type}` — mapped to a
`Map<string, string>` keyed by `loc.join(".")` and rendered next to the
matching input. A 409 means either a stale version (someone else saved
first — the config is silently reloaded to the current version) or a missing
voice consent (surfaced as the server's own actionable message, which names
the exact `onboard_tenant` command to run).

**The Hours section has two states** (Phase 9.4). It fetches
`GET /admin/api/tenants/{id}/calcom` on load and, when that comes back
`connected: true`, renders Cal.com's actual schedule **read-only** with a
"Managed in Cal.com" badge — the editable grid isn't shown at all, because
for a Cal.com-backed bot editing it does nothing. The Booking section hides
"Slot granularity" and "Lead time" in the same state, for the same reason;
"How far ahead to look" and "Options to offer at once" stay, since Cal.com has
no say in either. When the fetch **fails**, `calcom` stays `null` and the
editable grid renders — the safe direction, since the alternative is hiding
the only controls that work.

A `timezone_matches: false` in that response raises a mismatch warning inside
the Hours section. Cal.com has two timezones (the schedule's, which governs
availability, and the account profile's, which governs how the dashboard
displays bookings); this route can only see the first, so the warning tells
the operator to check the second by hand.

**The Danger Zone** (bottom of `Config.tsx`, operator-only, Phase 9 Part B)
is Archive/Restore/Purge — a thin client over `POST /admin/api/tenants/
{id}/archive`, `.../restore`, `.../purge`. Purge is disabled until the
tenant's own `status` is `"archived"`, and again until the operator types
the exact tenant id into a confirm field (`app/channels/admin.py` checks
the same match server-side — the client-side disable is convenience, not
the actual guard). See `infra/README.md`'s "Removing a bot" section for
what purge actually deletes and in what order.

## The "AI Prompt" tab (`views/SystemPrompt.tsx`)

Edits `TenantConfig.system_prompt_override` — a full, tenant-scoped
replacement for the shared `content/system-prompt.md` template
(`app/brain/prompts/system.py::render_system_prompt`). `GET
/tenants/{id}` returns `_rendered_system_prompt`: the *actually rendered*
prompt for that tenant right now (server-side string formatting only, no
extra I/O), so a tenant with no override yet edits real, fully-resolved
text instead of a blank box or the raw `${placeholder}` file. Saving with
an empty string is equivalent to saving `null` — both fall back to the
shared template on the next turn, no restart needed. An override still
runs through `safe_substitute()`, so leaving tokens like `${business_name}`
or `${business_hours}` in place keeps that part dynamic; removing them
just makes that section fixed text. Not operator-only — same category as
`greeting`/`persona`, not `voice.voice_id`/`mcp_servers`.
