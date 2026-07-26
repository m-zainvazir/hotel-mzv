# Phase 3 — Real critical-path tools

## Context

Phases 1–2 are done: the LangGraph brain answers typed chat *and* live voice through Vapi's
Custom-LLM mode. But every real-world action is still a stub — `StubBookingProvider` invents a
calendar, `StubNotifier` logs instead of texting, `StubEscalator` records an escalation nobody
receives. Plan §15 Phase 3 is what turns the demo into something a business could actually run,
and its acceptance criterion is: *a call books a real appointment, texts a real confirmation, and
an emergency transfers the caller while alerting on-call.*

Phase 3 has been blocked on one decision — the booking provider. **That decision is now made:
Cal.com**, not Google Calendar. The scaffolding for all three integrations already exists as
`NotImplementedError` stubs behind finished interfaces (`BookingProvider`, `Notifier`,
`Escalator`), so this phase is filling in known shapes, not designing new ones.

Two facts verified on this box that shape the work:
* `httpx` is already a core dependency and `VapiClient` already talks to Vapi over raw httpx.
  **Cal.com and Twilio need no new dependencies** — no SDKs, and none of the Windows
  Application Control risk that `uuid_utils` caused.
* `langchain-core` is 1.5.0, and a `response_format="content_and_artifact"` tool returns a plain
  string to a bare `.invoke({...})` while giving ToolNode a `ToolMessage` with `.artifact`
  (verified empirically). That makes the warm-transfer signal free of breaking changes.

## Decisions locked (do not revisit)

| | |
|---|---|
| Booking provider | **Cal.com**, behind the unchanged `BookingProvider` ABC. Google stays a future swap. |
| Event types | Default **one multiple-duration event type per tenant** (`booking.event_type_id`), with an optional per-service override (`Service.event_type_id`). Resolution: `service.event_type_id or tenant.booking.event_type_id`. |
| Guest email | Cal.com requires one. `book_job` gains an optional `customer_email`; otherwise synthesize deterministically from the phone. |
| Address | New `booking.require_address` (default `true`). Hotel sets `false`. |
| Twilio SMS | Implement for real — you are creating the account now. |
| Warm transfer | Implement fully, but escalation numbers stay fake `+1555…`. Verify by tests + `provision_vapi --dry-run`, not a live transferred call. |
| Pilot tenant | Convert `hotel-mzv` from electrician to a real hotel. |

## What I need from you (blocks steps 3, 4 and 8 only — 1, 2, 5, 6, 7 can proceed now)

**Cal.com** — free tier is enough; API keys are available on it.
1. An account, then **Settings → Developer → API Keys → New**. Copy it immediately; it is never shown again. → `CALCOM_API_KEY` in `.env`.
2. One event type for the hotel with **multiple durations enabled**, offering at least 15/30/60/90/240 minutes, and set to **auto-confirm** (not "requires confirmation" — otherwise we say "confirmed" when it isn't). Give me its numeric id → `booking.event_type_id`.
3. Its availability schedule in Cal.com should mirror the hotel's hours, because **Cal.com owns availability** (see Risk 1).

**Twilio**
4. `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and a real sending number (`TWILIO_FROM_NUMBER`) or a Messaging Service SID.
5. Tell me the country. If US, **start A2P 10DLC registration immediately** — it takes days, and unregistered long-code SMS is silently filtered (Twilio 30034: we get a 201, the guest gets nothing).
6. On a trial account you can only text **verified** numbers. Send me one verified handset number to test a real confirmation against.

Until 1–4 land, the seed tenants stay on `"provider": "stub"` and everything below is still
testable — going live is then a one-word JSON edit per tenant, no code change.

---

## Implementation

Each step ends with a green `pytest`. Steps 1, 2, 5, 6, 7 need no credentials.

### Step 0 — baseline
Run `pytest`, record the count.

### Step 1 — config and models only (no behaviour change)

`app/config.py` — add real `Settings` fields (they **must** be real fields; `hermetic_settings`
only strips env vars matching a field name, so a non-field leaks the dev box into the suite):
`calcom_api_key`, `calcom_api_base="https://api.cal.com/v2"`, `calcom_timeout_seconds=8.0`,
`booking_placeholder_email_domain="no-reply.aireceptionist.app"`, `twilio_api_base`,
`twilio_messaging_service_sid`, `twilio_whatsapp_from`, `twilio_timeout_seconds=8.0`.

**Delete `booking_provider` and `notifier_provider`** (`app/config.py:66-67`). Nothing reads them —
dispatch is per-tenant JSON in `app/tools/providers.py`. Two sources of truth for one decision is a
silent-failure trap. Remove them from `.env.example` too, with a comment saying dispatch is
per-tenant.

`app/tenancy/models.py` — `Service.event_type_id: int | None`; `BookingSettings.event_type_id`,
`.require_address: bool = True`, `.booking_field_map: dict[str,str]`;
`EmergencyPolicy.allow_warm_transfer: bool = True` (the kill switch for Risk 7).
Add a `TenantConfig` model validator: a `calcom` tenant with neither `booking.event_type_id` nor
per-service ids fails **at config load**, not mid-call.

`app/db/models.py` — `OutboundMessage` gains optional `provider_sid`, `status`, `error`.
`app/tools/booking/base.py` — `BookingRequest.customer_email`, and `address` defaults to `""`.

### Step 2 — shared HTTP client + a hard network guard

New `app/tools/http_client.py`: `shared_async_client(key, *, base_url, headers, timeout, auth,
transport)` memoised on `key`, plus `close_shared_clients()` and `reset_shared_clients()`.
Providers are constructed fresh on **every** tool call (`providers.py:27`), so without this a cold
TLS handshake to `api.cal.com` costs 150–300 ms of the §13 budget on every availability check.
Include a credential fingerprint in `key` so a `monkeypatch.setenv` can't reuse a stale client.
Wire `close_shared_clients()` into `app/main.py`'s `lifespan` (which currently has no shutdown
half) and `reset_shared_clients()` into `tests/conftest.py::isolated_runtime`.

Add an autouse `no_network` fixture patching `httpx.AsyncHTTPTransport.handle_async_request` and
`httpx.HTTPTransport.handle_request` to raise. Verified safe: `httpx.MockTransport` inherits from
`AsyncBaseTransport`, **not** `AsyncHTTPTransport`, so mocked tests are unaffected. Do this *before*
any HTTP code exists, so every later step is provably offline.

### Step 3 — Twilio notifier

`app/tools/messaging/twilio.py` — `TwilioNotifier` only (escalators move out in step 5).
`POST {twilio_api_base}/2010-04-01/Accounts/{sid}/Messages.json`, httpx basic auth, form body
`To`/`Body` plus **either** `MessagingServiceSid` (preferred — it survives A2P routing) **or**
`From = tenant.notifications.from_number or settings.twilio_from_number`. Missing creds →
`MessagingError` with no HTTP. Record an `OutboundMessage` row on failure too (`status="failed"`)
*before* raising, so the audit trail survives. **Log tenant/kind/sid/status only** — bodies carry
guest names and addresses.

Widen error handling in `app/tools/messaging_tools.py`:
* `send_confirmation` — guard the `.format()` (a tenant template typo is a live-call crash today)
  and the send. On failure return an `ERROR:` string that says the booking **is** confirmed and
  tells the model to read the details aloud instead — never "book again".
* `escalate` — **wrap the alert SMS so it can never take down the escalation.** Today it is
  unguarded and points at a fake `+1555…` number, so the moment Twilio goes live it will raise
  (error 21211) and kill the emergency path. This is the single most important fix in Phase 3.

### Step 4 — Cal.com booking provider

New `app/tools/booking/calcom.py`, mirroring `app/tools/booking/stub.py`'s structure. Constructor
takes `client=` for test injection; missing API key raises **lazily on first use**, so a
misconfigured tenant produces an `ERROR:` string mid-conversation rather than a 500.

`uses_shared_type = service.event_type_id is None` is the one rule driving both `duration` and
`lengthInMinutes`: send them only on the tenant-level multi-duration event type. Sending a
`duration` a fixed-length event type doesn't offer is the documented way to get zero slots back.

* **`check_availability`** → `GET /slots?eventTypeId=&start=&end=&timeZone=&format=range[&duration=]`,
  header `cal-api-version: 2024-09-04`. Window = `earliest or now` → `+ horizon_days`. Flatten
  `data.values()`, convert to tenant tz, sort, truncate to `max_slots_returned`. `"data": {}` → `[]`.
  **Do not re-filter by `lead_time_hours` / `slot_granularity_minutes`** — Cal.com owns availability
  and filtering with stale JSON is the cheapest way to produce a silently empty diary.
* **`create_booking`** → `POST /bookings`, header `cal-api-version: 2024-08-13`. Build the `Job`
  object first (unpersisted) so `job.id` can go into `metadata` as the reconciliation handle. Refuse
  naive datetimes; send `start` in **UTC**. Email = supplied or `caller-{digits}@{domain}`
  (deterministic, so a repeat caller is one Cal.com attendee). Address/notes go to `metadata` plus
  an optional `booking_field_map`-driven `bookingFieldsResponses` — empty by default, because the
  built-in field slugs are unverified and guessing them 400s every booking. On 201, take `uid` →
  `Job.calendar_event_id` and prefer the response's `start`/`end` over our arithmetic (this closes
  Risk 4). **Still write the local store row — it stays authoritative**; `send_confirmation` looks
  jobs up by id there, and plan §10 says Supabase holds the truth.
* **Error mapping** via one `_request()` helper: timeouts/transport/5xx/401/404 → `BookingError`;
  409 or a 4xx body matching "already booked/no longer available" → `SlotUnavailableError`. Never
  let raw provider text into the returned string — the model will read it aloud.
* `cancel`/`reschedule` minimally, marked ⚠️ VERIFY; no native tool calls them yet.

`app/tools/providers.py` — dispatch `"calcom"`.

`app/tools/booking_tools.py`:
* `book_job` gains `customer_email: str = ""` and `address: str = ""`; address validated only when
  `tenant.booking.require_address`; the `at {job.address}` suffix becomes conditional.
* **Widen the exception handling.** Today only `SlotUnavailableError` is caught, so a Cal.com 500
  or timeout escapes the tool, escapes ToolNode, and the caller gets `FALLBACK_LINE` with the
  booking silently lost. Add `BookingError` and a bare `Exception` guard, both returning an
  `ERROR:` string containing **"Do NOT say it is booked"** — Llama will otherwise cheerfully
  confirm a booking that never happened.
* `check_availability` has no try/except at all today; wrap it the same way.

### Step 5 — Vapi warm transfer

**How the runner learns an escalation happened, without naming a vendor:** `escalate` becomes a
`response_format="content_and_artifact"` tool returning `(text, {"kind": "handoff", "transfer":
…, "destination": …})`. The runner's existing `elif node == "tools":` branch reads
`message.artifact` and yields a new `BrainEvent("handoff")` (not `is_spoken`, so it can never
become audio).

Chosen over a `Command`-returning tool because state is checkpointed and additive — a
`pending_transfer` key would persist into later turns of the same call and could re-fire the
transfer. Artifacts die with the message. Chosen over sniffing the result string because that
couples the runner to prompt copy: one wording edit would silently disable emergency transfers.
**Verified**: a bare `.invoke({...})` still returns a plain `str`, so every existing `escalate`
test passes unchanged.

* New `app/tools/messaging/transfer.py`: `WarmTransferEscalator` (renamed from
  `VapiTransferEscalator` — under this design it contains nothing Vapi-specific) and
  `SmsCallbackEscalator` for chat (plan §8).
* `get_escalator(tenant, channel="chat")` becomes channel-aware: warm transfer only when
  `channel == "voice"` **and** `allow_warm_transfer` **and** an escalation phone exists. The call
  site in `messaging_tools.escalate` already has `channel`.
* `escalate`'s transfer wording must **always speak the number** ("if the line drops, call X
  directly") — a transfer can fail for reasons we cannot see (Risks 7, 8).
* `app/channels/vapi_schema.py` gains both Vapi wire shapes, per CLAUDE.md's rule that every Vapi
  assumption lives in this file: `transfer_call_tool(destinations)` for the assistant payload, and
  `transfer_call_chunk(destination)` → `{"function_call": {"name": "transferCall", "arguments":
  {"destination": {"type": "number", "number": …}}}}`. `openai_compat.py` stays untouched — it is
  the vendor-neutral file shared with Retell/Pipecat, and that frame is not an OpenAI chunk.
* `vapi_llm._sse_chunks` **buffers** the destination and emits the frame after all content, just
  before the terminal `stop` chunk. Ordering is the design: Vapi acts the instant the frame
  arrives, so emitting it when the tool returns would transfer the caller mid-sentence — that
  ordering *is* the "warm" in warm transfer. `_collect` (the non-streaming twin) logs a warning
  and behaves as today.
* `build_assistant_payload` adds `model["tools"] = [transfer_call_tool([escalation_phone])]` when
  enabled. Add a transfer line to `provision_vapi --show`.
* Add `escalate` to `SLOW_TOOLS` — it now does real network I/O, and dead air on the emergency
  path is the worst dead air there is. `content/acknowledgements.json` already has an unused
  `"escalate"` key. But make `acknowledgement_for` channel-aware first (`app/brain/acknowledge.py:49`
  currently does `del channel`): "putting you through now" is a **lie on chat**, and not telling
  that lie is the entire point of the `can_transfer` machinery.
* Update the two tests that assert the current "CANNOT transfer" wording.

### Step 6 — hotel tenant conversion (own commit — biggest diff, zero new logic)

Rewrite `content/tenants/hotel-mzv.json`: `trade: "hotel"`, daily 07:00–22:00, services
`room-reservation` 30m / `restaurant-table` 90m / `spa-treatment` 60m / `event-space` 240m /
`airport-transfer` 60m / `urgent-assistance` 15m, hotel emergency keywords (fire, smoke, medical,
unconscious, choking, intruder, stuck in the lift, carbon monoxide), `require_address: false`,
`event_type_id` present, and `confirmation_template` with `{address}` removed.

**Keep `provider: "stub"` in the committed seed** for both booking and notifications — flipping it
would route ~30 hermetic tests through the Cal.com dispatcher and break `chat_cli` on any box
without a key. **Keep the name "Hotel_MZV"** so `test_vapi_provisioning` survives untouched.
Keep a 240-minute service so `test_long_service_never_overruns_closing_time` still tests what it says.

Test files needing mechanical updates: `test_tenancy`, `test_emergency`, `test_booking_provider`,
`test_native_tools`, `test_vapi_llm`, `test_api`, `test_streaming`, `test_sanitize`,
`test_repeat_suppression`, `test_llm_cost`, `test_brain_end_to_end`, `test_tenant_isolation`, and
`tests/fixtures/vapi_chat_completion_request.json`. Two need thought rather than search-and-replace:
`test_native_tools`'s `("address", "")` case must move to a `require_address: true` tenant
(parametrise on `northside`), and `test_vapi_llm`'s `spoken()` helper (line 47) does
`chunk["choices"][0]` on every chunk — it must skip the `function_call` frame.

### Step 7 — docs
`CLAUDE.md` (booking default → Cal.com; unblock the Phase 3 note; add the "Cal.com owns
availability" gotcha), `content/README.md` (advisory hours/lead time; how to flip a tenant live),
`README.md`, and an amendment note on plan §10.

### Step 8 — live verification (needs credentials)
`provision_vapi --tenant hotel-mzv --dry-run` and eyeball `model.tools`; then a real run (one HTTP
call, no phone) to see whether Vapi accepts a `+1555…` destination. Flip a scratch tenant to
`calcom` and book once through `chat_cli`. Send one real SMS to your verified handset.

---

## Testing

`httpx.MockTransport` + constructor injection — no new dependency, consistent with "raw httpx, no
SDKs". Add a `mock_http(handler)` helper to `conftest.py` returning a client plus captured
requests, so tests assert on URL, params, headers and body as well as return values. The autouse
`no_network` guard from step 2 is the real insurance: a stray `"provider": "calcom"` in a committed
tenant file can never silently dial out from CI.

New files: `tests/test_calcom_booking.py` (request shape, `duration`/`lengthInMinutes` only on the
shared event type, tz conversion, truncation, empty data, deterministic email, `uid` → job, times
taken from the response, the full error-mapping table, and **missing key/event-type raises with
zero HTTP calls**), `tests/test_calcom_tools.py` (a `BookingError` becomes an `ERROR:` string and
never an exception — the dead-call guard; `require_address` both ways),
`tests/test_twilio_notifier.py` (form encoding, basic auth, messaging-service override, failure row
recorded, **and `escalate` still succeeds when the alert SMS raises**), `tests/test_warm_transfer.py`
(channel-aware escalator; `escalate` still returns a plain `str`; voice yields exactly one `handoff`
event and chat none; the `function_call` frame appears once, after all content, before `stop`; the
payload carries the number exactly once; chat acknowledgement never says "putting you through").

## Risks

1. **Cal.com owns availability, but the prompt still recites the tenant's `hours`.** If the two
   disagree the bot states hours it cannot book — the most confusing possible failure. Keep the JSON
   mirroring Cal.com, and consider annotating `${business_hours}` for calcom tenants.
2. **`lead_time_hours` / `slot_granularity_minutes` become no-ops** for calcom tenants. Someone will
   edit them and see nothing change. Document loudly; log once at provider construction.
3. **A timed-out `POST /bookings` may have succeeded** — a real event with no local row, and a guest
   told it failed. 10s timeout, `metadata.job_id` for reconciliation. Idempotency keys are Phase 4.
   Also: a "requires confirmation" event type makes us say "confirmed" when it isn't — hence the
   auto-confirm requirement above.
4. **`lengthInMinutes` is only honoured on multi-duration event types.** Mostly closed by taking
   `start`/`end` from the API response and warning on mismatch.
5. **DST / naive datetimes.** Always send UTC; refuse naive input. Unit-test a DST boundary.
6. **`"data": {}` is indistinguishable from a wrong `eventTypeId`** — both look like "no
   availability" and the bot politely offers a callback forever with nothing in the logs. Mitigated
   by the config-load validator plus a WARNING on a zero-slot full-horizon query.
7. **The `+1555…` transfer destination is fake.** Vapi may accept it at provisioning and fail at
   transfer time — the caller hears "transferring you" and gets dropped, on the emergency path.
   Mitigated by always speaking the number and by `allow_warm_transfer: false`.
8. **Re-provisioning is mandatory.** Vapi ignores a `function_call` for a destination the assistant
   never declared, so any tenant not re-run through `provision_vapi` gets a silent dead end.
9. **A2P 10DLC.** Unregistered US SMS is filtered while returning 201 — we think it sent.
10. **PII in logs.** Twilio bodies carry names and addresses; Cal.com payloads carry phone and
    email. Precedent to follow: `vapi_schema.redacted()`.

## Deferred to later phases
Google Calendar provider (interface already accommodates it), per-tenant Cal.com credentials —
today one global `CALCOM_API_KEY` means all tenants share a Cal.com account, isolated only by event
type id; per-tenant keys need the Phase 4 secrets vault. Booking idempotency keys, and a
`cancel`/`reschedule` native tool.
