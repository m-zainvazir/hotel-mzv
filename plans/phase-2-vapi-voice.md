# Phase 2 — Voice via Vapi Custom LLM

## Context

Phase 1 shipped a working brain: `resolve_tenant → emergency_check → reason ⇄ tools`,
streaming Groq tokens, five native tools, verified live booking a job end to end
(95 tests, ruff clean). It is reachable two ways today — `scripts/chat_cli.py` and
`POST /chat` (SSE) — both typed.

Phase 2 gives it a mouth and ears. Per plan §15, the goal is: *wrap the graph in an
OpenAI-compatible streaming `/chat/completions`, expose it via a tunnel, point a Vapi
assistant at it, and make a real phone call.* Done when you can call a number and hold a
natural, streamed conversation.

Two things shape this beyond the plan text:

1. **You want all three doors open and switchable** — typed chat, browser web call, real
   phone call — with none of them mandatory. This costs almost nothing, because the web
   call and the phone call are the *same Vapi assistant* hitting the *same* endpoint. The
   only difference is whether a phone number is attached. Switching is provisioning
   config, not code.
2. **Vapi is a thin shell by design** (CLAUDE.md #4). Everything added here is a
   re-encoder. No graph node may learn what Vapi is.

Decisions taken: stock Cartesia voice (latency + cost + Phase 4 cloning continuity),
ngrok tunnel, Twilio number import as an *optional flag* rather than a prerequisite.

## What exists to build on

| Thing | Where | Note |
|---|---|---|
| `stream_turn()` → `BrainEvent` stream | `app/brain/runner.py:43` | The single seam. Already emits `token`/`acknowledgement`/`tool_start`/`tool_result`/`final`/`error`. |
| SSE adapter pattern to copy | `app/channels/chat.py:39` | 12 lines. The Vapi shim is the same shape, different encoding. |
| Bearer auth dependency | `app/channels/security.py:17` | Extend with a Vapi-secret dependency; don't reuse the bearer one. |
| Tenant resolution | `app/tenancy/loader.py:resolve_tenant_id` | Already takes `phone_number` / `widget_key`. Add `assistant_id`. |
| Repository lookups | `app/tenancy/repository.py:find_by_phone` | Add `find_by_assistant_id` beside it. |
| In-memory store + protocols | `app/db/memory_store.py`, `app/db/store.py` | Mirror the `JobStore`/`MessageLog` pattern for a new `CallLog`. |
| Scripted model for tests | `tests/conftest.py:ScriptedChatModel` | Whole Phase 2 suite runs offline with this. |
| 501 stubs to replace | `app/channels/vapi_llm.py`, `app/channels/webhooks.py` | Already routed in `app/main.py:37`. |

## Scope

**In:** the SSE shim, tenant resolution from the Vapi payload, call-scoped conversation
threads, Vapi secret verification, call-record webhooks, assistant provisioning script,
config + tenant-model fields, tests, docs.

**Out (Phase 3):** warm transfer / `transferCall` — `escalate` keeps recording to the stub
escalator; real Google Calendar; real Twilio SMS. **Out (Phase 4):** voice cloning,
Supabase persistence.

## Implementation

### 1. Config and tenant model

`app/config.py` — add to `Settings`: `vapi_private_key`, `vapi_public_key`,
`vapi_webhook_secret`, `public_base_url`, `cartesia_api_key`, `cartesia_default_voice_id`,
`deepgram_api_key`. All already present in `.env.example`; they are simply not read yet.

`app/tenancy/models.py` — two new optional blocks on `TenantConfig`:

```python
class VoiceSettings(BaseModel):      # provider="cartesia", voice_id, model="sonic-2", speed
class VapiSettings(BaseModel):       # assistant_id, phone_number_id, max_duration_seconds
```

**Voice must stay config, never code.** Phase 2 ships a stock Cartesia voice, but changing
it later must be a one-line edit, so:

- `voice.voice_id` lives in the tenant JSON (`app/tenancy/data/<tenant>.json`), falling back
  to `CARTESIA_DEFAULT_VOICE_ID` when unset — so a new tenant inherits a sane default.
- `voice.provider` is part of the block too, not hard-coded. Swapping Cartesia for another
  Vapi-supported TTS provider is the same one-line edit, not a refactor.
- Changing either is: edit the JSON → re-run `provision_vapi.py --tenant <id>` → done. No
  redeploy, no code change. The provisioning script re-applies the whole voice block every
  run, which is why it must be idempotent.
- Phase 4 voice cloning then slots straight in: a clone is just a different `voice_id` on
  the same provider.

Add `find_by_assistant_id()` to `JsonFileTenantRepository` and an `assistant_id=` branch to
`resolve_tenant_id()`, ordered **assistant id → dialled number → default tenant**.

### 2. OpenAI wire format — `app/channels/openai_compat.py` (new)

Isolated from Vapi on purpose: Retell and Pipecat speak the same dialect, so swapping voice
vendors later touches only the file below this one.

- `chunk(id, content=None, finish_reason=None) -> dict` producing
  `{"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {...}, ...}]}`
- `completion(id, text) -> dict` for the non-streaming (`stream: false`) path.
- `sse(payload) -> str` and the `data: [DONE]` terminator.

### 3. The shim — `app/channels/vapi_llm.py`

`POST /chat/completions`, guarded by `require_vapi_secret`.

**Request handling.** Parse into a lenient model in `app/channels/vapi_schema.py` (new) —
lenient because unknown fields must never 422 a live call. Extract:

- `call.id` → thread key
- `call.assistantId` and the dialled number (`call.phoneNumber.number`) → tenant
- `messages` → the last `user` message is this turn's input

**Never read a tenant id from the request body.** The caller controls that payload.

**Thread continuity.** `thread_id = f"{tenant}:vapi:{call.id}"`, so a call resumes across
turns and two tenants can never collide. Vapi resends the whole history every turn while
our checkpointer already holds it — including the `ToolMessage`s Vapi cannot see. So:
feed only the newest user message normally, and **seed from Vapi's history only when the
thread is cold** (server restarted mid-call). Detect via `get_graph().aget_state(config)`.

This needs one small addition to `app/brain/runner.py`: an optional
`history: list[AnyMessage] | None` on `stream_turn`, prepended to the input messages. Drop
Vapi's `system` message entirely — `reason` renders the tenant's own system prompt, and
keeping both would let them contradict each other.

**Response mapping** — the rule is *only spoken events become audio*:

| `BrainEvent` | Emitted as |
|---|---|
| `token`, `acknowledgement` | `delta.content` chunk |
| `tool_start`, `tool_result` | **nothing** (logged only — these would be read aloud) |
| `final` | `finish_reason: "stop"` then `data: [DONE]` |
| `error` | a spoken apology as content, then a clean stop |

That last row matters: **never raise mid-stream.** An HTTP error leaves the caller in
silence. The `error` event must become an audible sentence.

### 4. Webhooks — `app/channels/webhooks.py`

`POST /webhooks/vapi`, same secret guard. Handle `status-update` (log) and
`end-of-call-report` (persist). Unknown message types return 200 and do nothing — Vapi adds
event types over time and an unknown one is not an error.

New `Call` model in `app/db/models.py` (tenant_id, vapi_call_id, from/to, started/ended,
duration, ended_reason, transcript, recording_url, cost, channel), a `CallLog` protocol in
`app/db/store.py`, and the implementation on `InMemoryStore` — same shape as jobs, so
Phase 4 swaps it wholesale.

### 5. Provisioning — `scripts/provision_vapi.py` (new)

This is where channel switching lives. Idempotent: writes `vapi.assistant_id` back into the
tenant JSON, so re-running updates the assistant rather than creating a duplicate (needed
every time the free ngrok URL rotates).

```
python -m scripts.provision_vapi --tenant acme-electric            # web-call only
python -m scripts.provision_vapi --tenant acme-electric --attach-number +1555...
python -m scripts.provision_vapi --tenant acme-electric --detach-number
python -m scripts.provision_vapi --tenant acme-electric --show
```

Assistant payload built from tenant config: `model.provider="custom-llm"` with
`url={PUBLIC_BASE_URL}`, `server.url={PUBLIC_BASE_URL}/webhooks/vapi` +
`server.secret`, `transcriber=deepgram`, `voice=cartesia` + `voice_id`, and
**`firstMessage = tenant.greeting`** so the greeting is spoken instantly with zero LLM
latency. Set `maxDurationSeconds` as a cost guard on test calls.

`--attach-number` imports a Twilio number when `TWILIO_ACCOUNT_SID`/`AUTH_TOKEN` are set,
otherwise provisions a Vapi-native one.

### 6. Docs

README: a "Talk to it" table covering all four doors (CLI, `/chat`, web call, phone) and
the ngrok runbook. CLAUDE.md: mark Phase 2 done, and record the two non-obvious
invariants — *tool events must never reach `delta.content`*, and *thread id is the Vapi
call id, seeded from Vapi history only when cold*.

## Verification

**Automated** (`pytest`, all offline via `ScriptedChatModel`):

- `tests/test_vapi_llm.py` — chunk schema valid; content arrives in order; final chunk
  carries `finish_reason: "stop"`; stream ends with `[DONE]`; **tool text never appears in
  any `delta.content`**; a brain `error` becomes spoken content and HTTP 200, not a 500;
  `stream: false` returns a well-formed completion.
- Tenant resolution — assistant id beats dialled number; unknown assistant falls back to
  the number; both unknown → default tenant; **a `tenant_id` in the request body is
  ignored**.
- Cold-thread seeding — a mid-call turn against an empty checkpointer reconstructs history
  from the payload and does not double it.
- Auth — missing/wrong secret → 401 on both endpoints.
- `tests/test_vapi_webhooks.py` — `end-of-call-report` persists a `Call` scoped to the right
  tenant; unknown message type → 200 no-op.
- `tests/fixtures/vapi_chat_completion_request.json` — captured real payload, replayed.
- Voice is swappable: a test asserting that changing `voice.voice_id` / `voice.provider` in
  tenant config changes the provisioning payload, with no code path branching on a
  hard-coded voice.
- Existing 95 tests still green; `ruff check .` clean.

**Manual** (the actual acceptance criterion):

1. `.venv\Scripts\python.exe -m uvicorn app.main:app --reload`
2. `ngrok http 8000` → put the https URL in `PUBLIC_BASE_URL`
3. `python -m scripts.provision_vapi --tenant acme-electric` → prints the assistant id
4. **Web call** from the Vapi dashboard: "my kitchen lights stopped working" → hear the
   greeting immediately, hear an acknowledgement before the availability lookup, book a
   job. Confirm with `/jobs` in the CLI (same store).
5. `--attach-number +1...` → **dial it from a real phone** and repeat. This is the
   Phase 2 acceptance criterion.
6. Hang up → confirm a `Call` record with transcript and duration.
7. Confirm `python -m scripts.chat_cli` still works — one brain, three doors.

**Latency spot-check** against the §13 budget: log a timestamp on request receipt and on
first `delta.content` flush; first token should leave inside ~400ms. Anything worse is
Groq or region placement, not the shim.

## Risks

| Risk | Mitigation |
|---|---|
| **Vapi payload/header shapes may differ from my Jan-2026 knowledge** (esp. the secret header name and `metadataSendMode`, which controls whether `call` is even sent) | Confirm against docs.vapi.ai *before* writing the parser. Keep every wire detail in `vapi_schema.py` + one header constant. Capture the first real payload into the fixture. This is the single most likely source of lost time. |
| ngrok free URL rotates on restart | Provisioning script is idempotent and re-runnable; one command to re-point. |
| Groq emits a malformed tool call mid-call | Already mitigated (`sanitize.py` + the retry in `reason.py`). On voice it degrades to a spoken clarifying question — verify this audibly during the manual run. |
| Runaway test-call cost | `maxDurationSeconds` on the assistant; prefer web calls for iteration (no telephony leg). |
| Double-counted history | Explicitly tested; drop Vapi's system message and seed only when cold. |

## Est. effort

2–3 days, matching plan §15. The shim is ~1 day; provisioning and the first real call are
where the time actually goes, because of the wire-format verification above.
