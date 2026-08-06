# Chat widget — Phase 5

An embeddable, dependency-free chat widget: one `<script>` tag, streaming
replies, quick-reply chips, and a click-to-call button on an emergency
handoff. It drives the same graph as every other door — a booking made here
is the same `jobs` row you'd get from the phone.

## Embedding it

```html
<script src="https://your-deployment.example/widget.js"
        data-widget-key="pk_widget_hotelmzv_demo"
        data-accent="#0f766e"></script>
```

| Attribute | Required | What it does |
|---|---|---|
| `data-widget-key` | yes (unless `data-test-token` is set) | The tenant's public widget key (`content/tenants/<id>.json` → `widget_keys`). The browser never sends a tenant id it chose itself — the backend maps this key to a tenant at `/chat/session` and everything after that is driven by a server-minted, tenant-scoped session token. |
| `data-accent` | no | Overrides the tenant's `chat.accent_color` for the launcher/header/chips. |
| `data-test-token` | no | Phase 9.1 — additive, never a replacement for `data-widget-key` above. Set by the Test Agent page (`GET /test/{token}`, minted from `/admin`'s "Test Agent" button) instead of a widget key: the handshake goes to `POST /test/session` rather than `/chat/session`, so a tenant with an empty `widget_keys[]` is still testable. Never used on a real embed. |
| `data-auto-open` | no | `"true"` opens the panel immediately instead of waiting for a launcher click — set by the Test Agent page, which has no launcher to click. |

**This script tag is the frozen contract.** Once a client site has it pasted
in, it can never change — you cannot ask them to re-paste it. Everything
behind it (the bundle, the framework, the internal wire format between
`/chat/session` and `/chat`) is free to change at any time; the tag itself is
not.

The launcher mounts into its own [Shadow
DOM](https://developer.mozilla.org/en-US/docs/Web/API/Web_components/Using_shadow_DOM),
so it can't be broken by the host page's CSS and can't leak styles into it
either. Including the script twice is safe — it mounts exactly one widget.

## How it works

1. On first open (not on page load — a visitor who never clicks the launcher
   costs nothing), the widget calls `POST /chat/session` with the widget key.
   The server resolves the tenant, checks `Origin` against
   `chat.allowed_origins` if that list is non-empty, and returns a
   `session_id` + a short-lived signed token, plus the tenant's greeting,
   accent color and service list (rendered as opening quick-reply chips).
2. Every message after that is `POST /chat` with `Authorization: Bearer
   <token>` — **never** a `tenant_id` in the body. The token is what the
   backend trusts for tenancy on this path; a value in the body is ignored
   for a widget caller. See `app/channels/chat.py`'s module docstring.
3. `/chat` streams SSE. `check_availability` results carry a `suggestions`
   event with real slots (`start_iso` + a human `label`) that render as
   chips; clicking one sends its label back as an ordinary message — never
   the raw ISO — so the model (which already holds the numbered list in its
   own context) is what actually calls `book_job`.
4. An emergency (`escalate`) produces a `handoff` event on every channel now;
   on chat `data.transfer` is always `false` (there's no live transfer to
   perform here — that's voice-only), so the widget renders a `tel:` link
   instead.
5. Phase 9.1: a tenant with a `links` catalog can have the model call
   `offer_actions`, producing an `actions` event — a `type: "link"` entry
   renders as a real `<a>` (opens in a new tab, never a URL pasted into the
   reply text); `type: "handoff"` renders a button that sends its label back
   as an ordinary message, driving the existing `escalate` tool the same way
   a quick-reply chip drives `book_job`.
6. The session persists across a page refresh via `sessionStorage`, keyed by
   widget key (or by the test token, on the Test Agent page).

## Buttons, flows and cards (Phase 9.2)

**No configuration is required for any of this.** `offer_actions` and
`offer_cards` are bound on every chat tenant, and every chat prompt gets a
shared "How this chat looks" section (`app/brain/prompts/system.py`), so a
bot renders buttons, quick replies and carousels purely from what its AI
prompt tells it to do. The model composes a button as `{label, url}` or
`{label, reply}`; the server validates the URL (`app/flows/urls.py`) and
returns a wire row.

A tenant *may* also declare a **button catalog** (`TenantConfig.links`,
under `/admin` → Config → Buttons) for the things that must be exact — a
URL that can't be wrong, a label that can't drift. The model then names a
`slug` instead. Both forms produce the identical row, and the widget
renders what arrives without ever looking anything up itself. Four types:

| Type | What a click does |
|---|---|
| `link` | Opens `url` in a new tab (`<a target="_blank" rel="noopener noreferrer">`). |
| `flow` | Sends the label as a message **plus** `postback: "flow:<id>"`. The server answers from config with **no LLM request at all** — see below. |
| `reply` | Sends `value` (or the label) as an ordinary message; the model answers it. |
| `handoff` | Identical to `reply` — the canned phrase is what drives the existing `escalate` tool, the same way a quick-reply chip drives `book_job`. |

**Flows** are scripted steps: fixed wording plus buttons, declared in
`TenantConfig.flows`. Clicking a flow button short-circuits
`app/brain/runner.py` into `app/flows/` before the graph is touched, so a
`🏠 Main Menu` button always says exactly the same thing, instantly, for
free. The model can also route *into* a flow when someone types free text,
via the `start_flow` tool — and `app/brain/graph.py::_after_tools` then ends
the turn, so it can't append a sentence of its own to a scripted node.

A flow turn is still written into the checkpointer
(`app/flows/render.py::_remember`), so the next free-text turn knows what
the visitor navigated through.

**The opening menu** has two routes. `chat.menu_flow`'s buttons come back
from the handshake as `tenant.menu` and render under the greeting
instantly, for free. Failing that, `ui.opening_turn` (on by default) makes
the widget run one real turn as the panel opens — the model can't produce
buttons without a turn to produce them in, so this is what lets a
zero-config bot greet people with its own buttons. The static `greeting`
bubble is suppressed in that case, and the server only asks for it when
there's no configured menu to use instead.

**Cards** (`offer_cards`, on by default via `ui.cards`) render a horizontal
carousel of image + title + subtitle + its own buttons. Card URLs are
model-supplied by necessity, so every one is scheme-checked and optionally
host-checked server-side before it reaches the browser — the widget assumes
nothing and validates nothing, deliberately, so there is one validator
rather than two that can disagree.

## Building

Requires Node.js 20+.

```bash
npm install
npm run build     # tsc --noEmit && vite build && writes dist/.buildhash
```

Outputs a single IIFE, `dist/widget.js` (Preact + everything else inlined,
CSS bundled and injected into the shadow root at runtime — no external
stylesheet, no code-split second request). **`dist/widget.js` and
`dist/.buildhash` are committed** (see the repo's `.gitignore`); `dist/`
itself is generated and `node_modules/` never is.

`dist/.buildhash` is a sha256 over every file under `src/` — `npm run build`
writes it, and `tests/test_widget_bundle.py` recomputes the same hash in
Python and fails if `dist/widget.js` has drifted from `src/` without a
rebuild. That's what lets a stale committed bundle get caught in `pytest`
without Node needing to be present in CI at all.

After changing anything under `src/`, **always** re-run `npm run build`
before committing — the hash guard exists specifically to catch forgetting
this.

## Developing without a client site

`GET /widget/demo` (served by `app/main.py`) is a static page that embeds the
widget the exact same way a real client site would — the same `<script>` tag,
nothing else. Start the backend (`uvicorn app.main:app --reload`) and open
`http://localhost:8000/widget/demo`.

```bash
curl -N -X POST http://localhost:8000/chat/session \
  -H "content-type: application/json" \
  -d '{"widget_key":"pk_widget_hotelmzv_demo"}'
# → {"session_id": "...", "token": "...", "tenant": {...}}

curl -N -X POST http://localhost:8000/chat \
  -H "content-type: application/json" \
  -H "authorization: Bearer <token from above>" \
  -d '{"message":"my kitchen lights stopped working"}'
```

## Source layout

```
widget/
  package.json  vite.config.ts  tsconfig.json
  src/
    main.ts          entry point: reads data-* attrs, mounts into a shadow root
    App.tsx           launcher + panel + message list + composer
    QuickReplies.tsx   the chip renderer (opening services + suggested slots)
    ActionButtons.tsx  renders offer_actions' link/handoff buttons (Phase 9.1)
    useStream.ts       fetch + ReadableStream SSE reader — the real hotspot;
                        EventSource can't POST, so this is hand-rolled framing
    api.ts             POST /chat/session and POST /chat (+ POST /test/session)
    styles.css          injected into the shadow root at runtime
  scripts/buildhash.mjs  writes dist/.buildhash (see "Building" above)
  dist/                  committed build output (widget.js + .buildhash only)
```
