# Phase 9.2 — Deterministic flows, rich buttons & generic-template cards

> **This reassigns the 9.2 slot.** `plans/phase9.1.md` and CLAUDE.md both earmark 9.2 for the
> *voice tester* (built on 9.1's signed-link + channel-flag seams). That work is not cancelled —
> it moves to **Phase 9.3**, and `app/main.py::_resolve_test_mode` keeps its existing "voice mode
> rejected, pending 9.2" branch with the comment updated to say 9.3. `ChannelToggle`'s docstring
> ("Phase 9.2's voice tester needs somewhere to hang `stt_provider`-shaped fields") needs the same
> one-word edit.

---

## Context

The bot can currently produce exactly three kinds of clickable thing, and every one of them is a
byproduct of a tool that exists for another reason:

| What renders | Where it comes from | Limitation |
|---|---|---|
| Quick-reply chips | `check_availability`'s `kind: "slots"` artifact | Only ever appointment slots |
| A `tel:` link | `escalate`'s `kind: "handoff"` artifact | Only ever an emergency number |
| Link / handoff buttons | Phase 9.1's `offer_actions` + `links` catalog | Opens a URL, or sends a canned phrase |
| An auto-detected URL button | the end-of-turn `extract_urls` fallback in `stream_turn` | Duplicates the URL already in the text |

What the two reference bots need, and none of the above provides:

1. **A persistent menu under the greeting** — five operator-authored buttons visible before the
   visitor types anything (`📅 Book an Appointment`, `📍 Find a Location`, …). Today the greeting
   renders `tenant.services` names as chips, which is a booking catalogue, not a menu.

2. **Deterministic flows.** The Plymouth prompt spends most of its length trying to make an LLM
   behave like a state machine — *"You must ONLY generate the following text message"*, *"Stop and
   WAIT"*, *"END OF TURN RULE"*, *"STRICT TERMINATION RULE"*. Those are all prompt-level pleas that
   a model obeys most of the time. A button labelled `🏠 Main Menu` should not be a probability.

3. **Generic-template cards.** An image + title + subtitle + its own buttons, in a horizontal
   carousel — the Amazon scraper bot's entire output format. There is no event type, no artifact
   kind, and no widget component for this.

**Outcome:** a tenant can declare a *flow graph* of scripted nodes (text + buttons) that render with
no model involvement at all; the model can hand off *into* a flow when someone types free text; and
either path can render a card carousel. All three reuse Phase 9.1's `links` catalog as the single
source of button truth.

### Decisions taken (from clarification)

| | |
|---|---|
| Postback behaviour | **Full deterministic flows** — a real flow engine, named nodes in tenant config, scripted messages with zero LLM involvement. Not LLM re-entry. |
| Card URL trust | **Per-tenant opt-in + host allowlist, *and* catalog-only buttons where required** — a card may carry model-supplied URLs (validated) *and* reference catalog slugs (resolved server-side) in the same item. |
| Prompt-override augmentation | **Build both, keep them switchable** — a per-tenant `prompt_augmentation` field, default `auto_append`, with the "require the placeholder" behaviour available as the other value. See [Deciding the prompt-augmentation question later](#deciding-the-prompt-augmentation-question-later). |

### What this phase deliberately does *not* build

- **Flow nodes never collect input.** A node is a leaf: text + buttons, nothing else. Plymouth's
  "Zip + Contact Capture" is *not* a flow — it's a `reply` button that hands control back to the
  LLM, which is genuinely better at open-ended capture than a rigid form would be. This is what
  keeps the engine ~150 lines instead of a second state machine living beside LangGraph's.
- **No branching/conditional logic inside a flow.** Branching is what buttons are.
- **Nothing reaches voice.** Flows, cards and the menu are chat-only, gated the same way
  `offer_actions` already is (`native_tools_for`'s `channel` parameter).
- **No migration.** `links`, `flows` and `cards` all live inside the `config` JSONB — they're
  outside `_TENANT_COLUMNS` (`app/tenancy/sync.py:31`), exactly as Phase 9.1's `links` note
  predicted. `sync.py`, `supabase_repository.py` and the draft/deploy path need **zero** changes.

---

## Architecture — four decisions worth stating before the steps

### D1. The flow engine lives in the brain, never in a channel

New package `app/flows/` (`resolver.py`, `render.py`). `stream_turn` gains a `postback: str | None`
parameter and short-circuits into it *before* touching the graph; `app/channels/chat.py` only
forwards a field off the request body. This is CLAUDE.md's "never put business logic in a channel
adapter" applied to the one feature most tempted to break it — and it means a flow turn still gets
rate limiting, transcript persistence, channel checks and the draft-preview override for free.

### D2. A flow turn must still write to the checkpointer

This is the single most important non-obvious detail. A deterministic node bypasses the graph, so
LangGraph's checkpointer never sees it — and the *next* free-text turn would then have amnesia about
the three buttons the visitor just navigated through. After emitting a node, write the exchange back:

```python
await get_graph().aupdate_state(
    thread_config(tenant_id, session_id, channel="chat"),
    {"messages": [HumanMessage(button_label), AIMessage(node.say)]},
)
```

Wrapped in the same never-raises posture as `_record_chat_message` — a checkpointer hiccup must
degrade the *next* turn's context, never break the button click itself.

### D3. `start_flow` is the LLM's way in, and a flow node ends the turn — as a graph edge

The model routes free text into a flow by calling a new native tool `start_flow(flow_id)`. Its
artifact is `{"kind": "flow", ...}`, and `app/brain/graph.py` gains a conditional edge so that
artifact routes to `END` instead of back to `reason`:

```python
builder.add_conditional_edges("tools", _after_tools, {"reason": "reason", END: END})
```

Making termination a *graph edge* rather than a prompt instruction is the honest fix for what the
Plymouth prompt is trying to achieve with capital letters. It's also the smallest possible change to
a graph that has been `tools → reason` unconditionally since Phase 1 — `_after_tools` returns
`"reason"` for every artifact kind except `"flow"`, so nothing else in the graph changes behaviour.

### D4. One catalog, one button schema, four render targets

Phase 9.1's `links` catalog stays the single source of truth. Flow nodes reference **slugs**, not
inline button definitions — so `🏠 Main Menu` is declared once, not repeated in eight nodes. The
same resolved `{type, label, url, slug, ...}` row is what the widget renders, whether it came from
the greeting menu, a flow node, `offer_actions`, or a card's button list.

```
                    TenantConfig.links  (the catalog)
                             │
      ┌──────────────┬───────┴────────┬──────────────────┐
  greeting menu   flow node        offer_actions      card buttons
 (chat.menu_flow) (buttons:[slug])  (model picks)     (slug or model url)
      └──────────────┴────────────────┴──────────────────┘
                             │
                   ActionButtons / Cards  (widget)
```

---

## Feature 1 — The button catalog grows two types

### `app/tenancy/models.py`

`TenantLink` gains two members of the `type` literal and two optional fields:

```python
class TenantLink(BaseModel):
    slug: str
    label: str
    url: str | None = None            # type="link"
    value: str | None = None          # type="reply" — text sent as a user message; defaults to label
    flow: str | None = None           # type="flow"  — target FlowNode.id
    description: str = ""
    type: Literal["link", "handoff", "reply", "flow"] = "link"
```

Validators (extending the existing `_link_type_needs_a_url`, same fail-at-config-load posture as
`_calcom_tenants_declare_event_types`):

- `type="link"` still requires an `http(s)` `url`
- `type="flow"` requires `flow`
- a new **`TenantConfig`-level** validator `_flow_buttons_resolve`: every `link.flow` and every
  `FlowNode.buttons` slug must name something that actually exists. This has to live on the parent
  because a `TenantLink` can't see the flow list. A dangling reference is a dead button — catching
  it at validation means the admin panel 422s with a `loc` path pointing at the exact field, which
  is the whole point of Phase 8's "Pydantic is the entire validation layer" design.

New models:

```python
class FlowNode(BaseModel):
    id: str                           # ^[a-z0-9][a-z0-9-]{0,47}$ — reuse _LINK_SLUG_RE's shape
    say: str                          # rendered verbatim; NOT a model prompt
    buttons: list[str] = []           # catalog slugs, rendered in order
    description: str = ""             # what the model reads to decide whether to start_flow here

class CardSettings(BaseModel):
    enabled: bool = False             # off by default — no existing tenant changes
    allowed_hosts: list[str] = []     # empty = any http(s) host; the opt-in was the decision
    max_cards: int = Field(default=10, gt=0, le=25)

class TenantConfig(BaseModel):
    ...
    flows: list[FlowNode] = []
    cards: CardSettings = Field(default_factory=CardSettings)
    prompt_augmentation: Literal["auto_append", "placeholder_only"] = "auto_append"
```

Plus `ChatSettings.menu_flow: str | None = None` — the flow whose buttons render under the greeting.
It sits on `ChatSettings` because it's genuinely display-only (which node the *handshake* renders);
`flows` itself sits on `TenantConfig` because it does reach the graph, which is the line
`ChatSettings`' own docstring draws.

`prompt_augmentation` goes on `TenantConfig`, **not** `ChatSettings`, for the same reason — the
prompt is the graph.

### Files
- `app/tenancy/models.py` — the models above + validators
- `tests/test_tenant_config.py` — validator coverage, dangling-slug and dangling-flow cases

---

## Feature 2 — The flow engine

### `app/flows/resolver.py`

```python
@dataclass(frozen=True, slots=True)
class ResolvedFlow:
    node: FlowNode
    buttons: list[dict]        # fully resolved {type,label,url,slug,value,flow}

def resolve_flow(tenant: TenantConfig, flow_id: str) -> ResolvedFlow | None
def resolve_buttons(tenant: TenantConfig, slugs: Sequence[str]) -> list[dict]
def resolve_menu(tenant: TenantConfig) -> list[dict]      # chat.menu_flow's buttons, or []
```

`resolve_buttons` is the **one** slug→row resolver in the codebase. `app/tools/action_tools.py`'s
`offer_actions` currently inlines this loop; it gets refactored to call this instead — same
drop-unknown-slugs-with-a-warning behaviour, one implementation. This is the same "both call sites
share one function" guarantee that `native_tools_for` gives `reason` and `tools`, and that
`tenant_config_from_runnable` gives the four draft-preview call sites.

### `app/flows/render.py`

```python
async def stream_flow(
    tenant: TenantConfig, flow_id: str, *, session_id: str, label: str
) -> AsyncIterator[BrainEvent]
```

Yields, in order: one `BrainEvent("token", node.say)` (whole, not chunked — there's nothing to
stream, it's a fixed string), one `BrainEvent("actions", data={"actions": buttons})` when the node
has buttons, then `BrainEvent("final", node.say)`. Then performs D2's `aupdate_state` write-back.

An unknown `flow_id` yields nothing and returns `None`-ish so the caller falls through to the normal
LLM path — a stale button in a visitor's open tab after a deploy removed that node must degrade to
"the model answers it", never to a dead turn.

### `app/brain/runner.py`

```python
async def stream_turn(*, text, ..., postback: str | None = None) -> AsyncIterator[BrainEvent]:
    ...
    if postback and channel == "chat":
        flow_id = postback.removeprefix("flow:")
        if resolve_flow(tenant, flow_id):
            async for event in stream_flow(...):
                yield event
            return
    # otherwise: unchanged, straight into the graph
```

Placed **after** tenant resolution and after the draft-preview override is resolved, so a "Preview
draft" session navigating flows sees the *draft's* flows. Placed **before** the graph so a flow turn
costs zero LLM requests.

### `app/tools/flow_tools.py` (new) — `start_flow`

```python
@tool(response_format="content_and_artifact")
async def start_flow(flow_id: str, config: RunnableConfig = None) -> tuple[str, dict]:
    """Hand this conversation to one of the scripted flows listed in "Flows you
    can start". Call this the moment the caller's intent matches one — the flow's
    own message and buttons are shown automatically, so say nothing else."""
```

Returns `(node.say, {"kind": "flow", "flow_id": ..., "say": ..., "actions": [...]})`. The runner
reads the artifact (new `_flow_artifact`, alongside the existing `_handoff_artifact` /
`_actions_artifact` / `_suggestions_artifact`), emits `token` + `actions`, and D3's graph edge ends
the turn so the model never gets a chance to add a trailing sentence.

Bound in `native_tools_for` when `tenant.flows and channel == "chat"` — the third conditional native
tool, same shape as `search_knowledge` and `offer_actions`. Not in `SLOW_TOOLS` (pure in-memory
lookup, same reasoning `offer_actions` already documents).

### `app/brain/graph.py`

```python
def _after_tools(state) -> str:
    """A flow node is terminal: it says exactly what its config says and stops.
    Every other tool result returns to `reason`, exactly as before."""
```

### `app/channels/chat.py`

- `ChatRequest` gains `postback: str | None = None`, forwarded to `stream_turn`.
  No new trust surface: a postback names a flow id resolved against *the token's own tenant's*
  config, so a forged one can only ever reach flows that tenant already published.
- `ChatSessionTenant` gains `menu: list[dict]` from `resolve_menu(tenant)`. Additive — the widget's
  frozen `<script>` contract is untouched; this is the internal handshake shape, which
  `widget/README.md` explicitly says is free to change.

### Files
- `app/flows/__init__.py`, `resolver.py`, `render.py` (new)
- `app/tools/flow_tools.py` (new), `app/tools/action_tools.py` (refactor to `resolve_buttons`)
- `app/tools/registry.py`, `app/brain/graph.py`, `app/brain/runner.py`, `app/channels/chat.py`
- `tests/test_flows.py` (new), `tests/test_flow_tools.py` (new)

---

## Feature 3 — Generic-template cards

### `app/tools/card_tools.py` (new) — `offer_cards`

```python
class CardButtonSpec(BaseModel):
    label: str | None = None
    url: str | None = None      # model-supplied — host-validated
    slug: str | None = None     # catalog entry — resolved server-side; wins over url

class CardSpec(BaseModel):
    title: str
    subtitle: str = ""
    image_url: str | None = None
    url: str | None = None      # tapping the card body
    buttons: list[CardButtonSpec] = []

@tool(response_format="content_and_artifact")
async def offer_cards(items: list[CardSpec], config: RunnableConfig = None) -> tuple[str, dict]:
```

Server-side sanitation, in `app/flows/cards.py` so it's testable without a model in the loop:

| Rule | Why |
|---|---|
| `http(s)` scheme only, on every `url` and `image_url` | `javascript:`/`data:` in an `<a href>` is XSS on the *client's* page |
| Host must match `cards.allowed_hosts` when non-empty (exact or `*.suffix`) | The user's "+ catalog-only where required" answer, at host granularity |
| A `slug` button resolves through `resolve_buttons` and is **always** allowed | Catalog entries were operator-authored; the allowlist is about model-supplied URLs |
| Truncate to `cards.max_cards`; drop an item with no `title` | A runaway tool result must not produce a 200-card carousel |
| A card whose every field fails validation is dropped, not fatal | Same "partial answer beats a failed turn" posture as `offer_actions`' unknown slugs |

Emits `BrainEvent("cards", data={"cards": [...]})` — a new `EventType`, added to
`_PUBLIC_EVENT_TYPES` in `chat.py` and to the widget's `BrainEventType` union.

Bound when `tenant.cards.enabled and channel == "chat"`. Fourth conditional native tool. **Is** in
`SLOW_TOOLS`? No — it's an in-memory transform, like `offer_actions`. The *upstream* MCP search that
produced the data is already slow-by-default via `is_slow_tool`'s "anything not in the fixed five"
fallback, so the acknowledgement fires where it should.

### Files
- `app/tools/card_tools.py`, `app/flows/cards.py` (new)
- `app/brain/runner.py` (`_cards_artifact`, `"cards"` in `EventType`), `app/channels/chat.py`
- `tests/test_card_tools.py` (new)

---

## Feature 4 — Prompt rendering

`app/brain/prompts/system.py` gains two rendered sections, following the exact `${knowledge_rule}`
convention (empty string when the feature is off, contributing nothing but a blank line):

- `${links}` — **extended** to show each entry's type, so the model can tell a URL button from a
  postback: `  - main-menu (flow → main-menu): 🏠 Main Menu`
- `${flows}` — `## Flows you can start`, listing `id`, `description` and the node's first line
- `${cards_rule}` — the `offer_cards` guidance, only when `tenant.cards.enabled`

`content/system-prompt.md` gets `${flows}` and `${cards_rule}` placeholders plus two "How you work"
bullets (start a flow the moment intent matches; never describe a button in prose).

### The prompt-override problem

A pasted Botsify-style prompt (`system_prompt_override`) contains none of these placeholders, so the
model would never learn the catalog exists — which is exactly the situation for both example bots.

`render_system_prompt` therefore ends with:

```python
if tenant.prompt_augmentation == "auto_append":
    for section in (links, flows, cards_rule):
        if section and section not in rendered:
            rendered += "\n\n" + section
```

Substring check against the *rendered* text, so an override that *does* use `${links}` is left
completely alone — placement stays the operator's choice, and the append only fires when the section
would otherwise be missing entirely.

### Deciding the prompt-augmentation question later

You asked to keep both options open. Both ship, switchable per tenant via `prompt_augmentation`
(default `auto_append`), and the admin AI Prompt tab shows a warning banner whenever a catalog
exists but the override has no `${links}` — that banner is useful under either choice.

**When you want to settle it, say one of:**
- `"Lock prompt augmentation to auto_append — drop the toggle"`
- `"Lock prompt augmentation to placeholder_only — drop the toggle"`

Either one is a ~20-line removal: delete the field, delete the branch, delete the admin selector,
keep the banner.

### Files
- `app/brain/prompts/system.py`, `content/system-prompt.md`, `tests/test_system_prompt.py`

---

## Feature 5 — Widget

| File | Change |
|---|---|
| `widget/src/ActionButtons.tsx` | Render all four types. `link` → `<a target="_blank" rel="noopener noreferrer">` (unchanged). `handoff`/`reply` → button that sends `value ?? label`. `flow` → button that sends `{message: label, postback: "flow:<id>"}`. |
| `widget/src/Cards.tsx` (new) | Horizontal scroll-snap carousel. `<img loading="lazy" referrerpolicy="no-referrer">` in a fixed aspect box with `object-fit: cover`; broken image → `onError` hides the `<img>`, card keeps its text. Prev/next buttons like the screenshot; keyboard-reachable; `overflow-x: auto` so it never widens the panel. |
| `widget/src/App.tsx` | `send(text, opts?: {postback?: string})`; `cards` in `applyEvent`; render `session.tenant.menu` under the greeting bubble — **replacing** the services chips when `menu` is non-empty, so a tenant with no menu keeps today's behaviour byte for byte. |
| `widget/src/api.ts` | `sendMessage(..., postback?)`; `menu` on `ChatSessionTenant`. |
| `widget/src/useStream.ts` | `"cards"` in `BrainEventType`. |
| `widget/src/styles.css` | Carousel + a stacked full-width button variant (the screenshots stack, today's chips wrap inline). |

**Rebuild the bundle** — `npm --prefix widget install && npm --prefix widget run build`, and commit
`widget/dist/widget.js` + `.buildhash`, or `tests/test_widget_bundle.py` fails.

---

## Feature 6 — Admin panel

`admin/src/views/Config.tsx`:
- **Links section** — the `type` select gains `reply` and `flow`; conditional `value` / `flow`
  fields (a `<select>` over the tenant's own flow ids, so a dangling reference is unpickable rather
  than merely 422-able)
- **Flows section** (new) — add/remove/reorder nodes; per node: id, description, `say` textarea, and
  an ordered multi-select of catalog slugs for buttons
- **Cards section** (new) — `enabled`, `allowed_hosts` (one per line), `max_cards`
- **Chat section** — a `menu_flow` selector

`admin/src/views/SystemPrompt.tsx` — the `${links}` warning banner + the `prompt_augmentation`
selector.

Everything routes through the existing Phase 9.1 draft → Deploy path with no changes: these are
plain `TenantConfig` fields, `put_tenant`'s shallow top-level merge already handles them, and
`_OPERATOR_ONLY_PATHS` needs **no** new entry (a tenant editing its own buttons is ordinary
self-service, unlike `widget_keys` or `vapi`).

**Rebuild** — `npm --prefix admin run build`, commit `admin/dist/` + `.buildhash`
(`tests/test_admin_bundle.py`).

---

## Testing

New: `tests/test_flows.py`, `tests/test_flow_tools.py`, `tests/test_card_tools.py`. Extend
`tests/test_action_tools.py`, `tests/test_system_prompt.py`, `tests/test_api.py`.

The ones that actually matter, beyond the obvious:

- **A flow turn makes zero LLM requests.** Assert against the `scripted` fixture's call count — this
  is the entire justification for the feature over LLM re-entry.
- **A flow turn is visible to the *next* free-text turn** (D2). Click a flow button, then send free
  text, and assert the model's message list contains the flow's `say`. Without the `aupdate_state`
  write-back this silently passes as "the model just doesn't mention it".
- **`start_flow` genuinely terminates the graph** (D3) — a scripted model that tries to keep talking
  after the tool returns produces no further `token` events.
- **Tenant A's postback can't reach tenant B's flow** — the mirror of
  `test_tenant_a_cannot_resolve_tenant_bs_slugs`.
- **A `javascript:` image/url in a card is dropped**, and an off-allowlist host is dropped while a
  catalog `slug` button on the same card survives.
- **A postback on `channel="voice"` is ignored** and falls through to the graph.
- **A stale postback (deleted flow) falls through to the LLM**, doesn't 500 or dead-end.
- **A tenant with no `flows`/`cards`/`menu` behaves byte-identically to today** — the guard that
  makes this phase safe to deploy to `hotel-mzv` and `northside-plumbing` untouched.

---

## Verification (live, against the real Supabase project)

Offline first: `pytest`, `ruff check .`, `ruff format .`, both bundles rebuilt.

Then the same loop we've been using — the dev server on `:8020` against the real project, driven
through `/admin` and a Test Agent link, clicked by you:

1. **Build the Plymouth bot for real.** Create a tenant through `/admin` → New Bot, paste
   `botsify/Playmouth.txt` into the AI Prompt tab, and add the catalog + flows it implies
   (`book-appointment`, `find-location`, `explore-services`, `workshops`, `careers`, `main-menu`;
   flows `main-menu`, `booking`, `locations`). Set `chat.menu_flow: "main-menu"`. **Save as draft,
   Preview draft** — this exercises Phase 9.1's draft path against brand-new field types.
2. **Screenshot 1 parity** — open the Test Agent link, confirm five buttons under the greeting
   before typing anything.
3. **Screenshot 2 parity** — click `📍 Find a Location`; confirm the exact configured sentence, the
   three buttons in configured order, and (in the server log) **no LLM request for that turn**.
4. **The LLM-routing path** — type "I need to find a clinic near me" as free text and confirm the
   model calls `start_flow` and lands in the same node, with nothing appended after it.
5. **Round-trip context** — after step 3, type "what were those options again?" and confirm the
   model knows what it just showed (proves D2 against a real Postgres checkpointer).
6. **Deploy**, and confirm the live link shows the same thing.
7. **Cards** — enable `cards` on a scratch tenant and drive `offer_cards`. **Note:** the Amazon bot
   needs a Tavily MCP key, which is `plans/phase10.md` item 11 and still blocked. Prove the
   carousel against `scripts/demo_mcp_server.py` (or a canned tool result) with real remote image
   URLs instead — the card *rendering* is what's being verified, and it's identical either way.
   Verify the allowlist by adding an off-list host and watching that card's button vanish while its
   catalog button survives.
8. **Archive the scratch tenants** through the Phase 9 Part B lifecycle path when done.

---

## Docs to update when it's done

- `plans/phase9.2.md` — this file, with a live-verification record appended (the convention every
  prior phase doc follows)
- `plans/phase9.1.md` + CLAUDE.md — the 9.2 → 9.3 voice-tester reassignment
- `widget/README.md` — a flows/cards/menu section beside the existing Phase 9.1 one
- `content/README.md` — how to author `links` / `flows` / `cards` by hand
- `CLAUDE.md` — Current state + any gotcha the live pass turns up (the `aupdate_state` write-back
  and the `tools → END` edge are both strong candidates)

---

## Implementation record (code-complete, offline-tested — NOT live-verified)

Built as planned, with the deviations and discoveries below.

### What changed versus the plan

| Plan said | What shipped | Why |
|---|---|---|
| `render.py::stream_flow(tenant, flow_id, ...)` | `stream_flow(tenant, resolved, ...)` | The caller already resolved the node to decide whether to short-circuit at all; re-resolving inside would be a second lookup that could disagree with the first. |
| `BrainEvent` stays in `runner.py` | Moved to new `app/brain/events.py`, re-exported from `runner` | `app/flows/` produces events and `runner` consumes `app/flows/` — leaving it in place was a circular import. Every existing `from app.brain.runner import BrainEvent` still works. |
| — | `content/templates/clinic.json` gained a full menu + booking + locations flow set | The plan's live-verification steps involve hand-building the whole Plymouth catalog through the admin UI. A worked template makes that a fraction of the clicking, and it's the natural home for the example. |

### The pre-existing bug this phase surfaced

`is_slow_tool` (`app/tools/registry.py`) inverted its rule as *"anything not in the fixed
five `NATIVE_TOOLS` is slow"*. That's right for MCP tools — the case it was written for,
where names can't be enumerated up front — and wrong for every **conditional native** tool,
none of which are in that constant either.

Consequence: `offer_actions` has been emitting a spoken acknowledgement ("Bear with me a
second…") before an instant in-memory dict lookup since Phase 9.1, while `registry.py`'s own
docstring asserted the exact opposite. Cosmetic in chat — which is why it survived a whole
phase unnoticed.

It stopped being cosmetic here: an acknowledgement before `start_flow` prefixes a
deterministic node's configured wording with model-generated filler, which is precisely the
property the feature exists to guarantee. `test_flow_tools.py::test_the_model_gets_no_turn_after_a_flow_renders`
caught it on first run by asserting the spoken text *equals* the node's `say`.

Fixed with a new `ALL_NATIVE_TOOLS` constant. `NATIVE_TOOLS` deliberately stays frozen at
five so `test_native_tools.py::test_critical_path_tools_are_all_native` keeps meaning what
it says.

### Test count

947 passing, `ruff check` and `ruff format` clean, both bundles rebuilt and committed.
New: 20 in `test_flows.py`, 11 in `test_flow_tools.py`, 28 in `test_card_tools.py`, plus
extensions to `test_action_tools.py` (extended button types, cross-reference validation) and
`test_system_prompt.py` (catalog rendering, augmentation).

`test_the_flow_turn_is_visible_to_the_next_free_text_turn` was verified to actually fail
when `_remember` is disabled — a test for a silent-degradation bug is worth nothing unless
you've watched it go red.

### Still owed

Everything in the Verification section above. Nothing has been rendered in a real browser,
no flow has run against the real Supabase project, and no card carousel has been seen. The
`clinic.json` template exists to make that pass quick.

---

## Amendment — model-authored UI (the zero-config reversal)

Reviewing the built feature against the project's actual premise surfaced a
mismatch worth naming plainly: **as originally built, 9.2 could not deliver the
thing it was for.** Buttons required a `links` catalog and cards required
`cards.enabled`, so a bot whose operator wrote only an AI prompt rendered plain
text — exactly the outcome the phase existed to fix. The catalog was solving for
safety; it was not solving for the stated goal ("the only input from my side
should be an AI prompt").

### What changed

| Before | After |
|---|---|
| `offer_actions(slugs: list[str])` | `offer_actions(buttons: list[ActionButton])` — `{label,url}`, `{label,reply}` or `{slug}` |
| Bound only when `tenant.links` non-empty | Bound on **every** chat tenant |
| `offer_cards` bound only when `cards.enabled` (default false) | Bound on every chat tenant; `ui.cards` defaults **true** |
| `CardSettings` (`enabled`, `allowed_hosts`, `max_cards`) | `UiSettings` (`buttons`, `cards`, `allowed_hosts`, `max_cards`, `opening_turn`) — all flags default on |
| `${cards_rule}`, only when cards were on | `${ui_rule}` — a fixed "How this chat looks" briefing on every chat prompt, auto-appended to overrides |
| Opening menu required `chat.menu_flow` | `ui.opening_turn` runs one real turn as the panel opens, so the menu can come from the prompt |
| Host/scheme checks lived in `app/flows/cards.py` | Extracted to `app/flows/urls.py`, shared by buttons and cards |

Flows are untouched. They remain config-only and deterministic, which is now
positioned as the *precision* path — for a Main Menu that must always say the
same words — rather than the only way to get a button.

### The trade-off, recorded

The slug indirection guaranteed a URL from a poisoned knowledge chunk or a
hostile tool result could never become a clickable `<a href>` on a client's own
website. That guarantee is gone. What stands in its place:

1. `http(s)` schemes only, always, regardless of settings (`app/flows/urls.py`).
2. `ui.allowed_hosts`, per tenant, empty by default.
3. Catalog slugs still bypass the allowlist — it constrains the model, never the
   operator.

This was an explicit decision, not an oversight. It is the same exposure every
comparable platform carries, and the alternative was a feature that didn't do
what it was built for.

### What offline tests still cannot tell us

963 pass, including a full zero-config path: no catalog, no flows, model composes
a button, it renders through `/chat`. But the scripted model does whatever a test
tells it to — so **none of this is evidence that a real model reliably calls
`offer_actions` unprompted.** That's a prompt-quality question, and `_UI_RULE` is
the first draft of an answer. Expect to tune it against real conversations; it's
one string in `app/brain/prompts/system.py`, deliberately.
