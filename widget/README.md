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
| `data-widget-key` | yes | The tenant's public widget key (`content/tenants/<id>.json` → `widget_keys`). The browser never sends a tenant id it chose itself — the backend maps this key to a tenant at `/chat/session` and everything after that is driven by a server-minted, tenant-scoped session token. |
| `data-accent` | no | Overrides the tenant's `chat.accent_color` for the launcher/header/chips. |

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
5. The session persists across a page refresh via `sessionStorage`, keyed by
   widget key.

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
    useStream.ts       fetch + ReadableStream SSE reader — the real hotspot;
                        EventSource can't POST, so this is hand-rolled framing
    api.ts             POST /chat/session and POST /chat
    styles.css          injected into the shadow root at runtime
  scripts/buildhash.mjs  writes dist/.buildhash (see "Building" above)
  dist/                  committed build output (widget.js + .buildhash only)
```
