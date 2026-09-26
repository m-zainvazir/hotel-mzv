# Phase 9.3 — Voice tester (browser mic → our own STT/LLM/TTS relay)

> **Slot history:** this was written as "Phase 9.2" inside `plans/phase9.1.md`. That slot was
> reassigned to flows/buttons/cards (`plans/phase9.2.md`), so the voice tester is 9.3. Every
> seam it depends on shipped in 9.1 exactly as designed and is now live-verified: the `mode`
> claim on test links, `ChannelToggle`, and the channel-flag gating.

---

## Context

An operator can test a bot's **chat** in one click (`/test/{token}`), and 9.1/9.2 made that
surface genuinely good — draft preview, flows, buttons, cards. There is no equivalent for
**voice**. Today the only way to hear a bot is to place a real Vapi web call, which means
provisioning an assistant, and it exercises Vapi's stack rather than ours.

Two consequences, and the second is the real motivation:

1. **Iterating on voice is slow.** Change a prompt, re-provision, place a call.
2. **We don't own the voice path.** Everything between the caller's microphone and
   `stream_turn` belongs to Vapi. We can't measure our own first-audio latency, can't swap
   STT/TTS providers, and can't offer voice at all to a client who doesn't want Vapi. The
   §13 budget (600–800ms end-of-speech → first audio) is currently something we *hope*
   holds, not something we measure.

Building our own relay fixes both, and it does so without new infrastructure: the server is
a **byte relay**, not a media processor.

`mode: "voice"` is already minted-and-rejected by `app/main.py::_resolve_test_mode` — this
phase makes it work.

### Explicitly NOT in this phase

- **Replacing Vapi for real phone calls.** PSTN stays with Vapi. This is a *tester* and a
  provider seam, not a migration. `vapi_llm.py`, `vapi_schema.py` and `webhooks.py` are
  untouched.
- **WebRTC.** See D1.
> **Superseded (26 Sep 2026): the mic is now a switch and endpointing is automatic.**
> Push-to-talk shipped, was used, and was rejected in exactly the terms this section
> anticipated — "I shouldn't have to press it all the time". `mic_on`/`mic_off` keep the
> STT stream open for the whole conversation and Deepgram's own `endpointing` /
> `utterance_end_ms` end each utterance. What stayed true is the reasoning below about
> *where* that decision belongs: it is still not a browser VAD and still not a timer, it
> is the provider reading the audio it already has. Barge-in kept its scope — cancel while
> speaking — but an utterance arriving while the bot is merely *thinking* is now queued
> rather than cancelled, because an always-on mic hears coughs.

- **Full-duplex barge-in.** The line is worth drawing precisely, because the offline
  verification below *does* test a "barge-in mid-speech" case and that is not a
  contradiction. **In scope:** cancelling a turn already in flight — the client sends
  `cancel` (D6), the server stops emitting audio frames, abandons the in-flight TTS and
  drops the rest of the reply. **Out of scope:** the *browser* deciding on its own, from
  voice activity, that the user has started talking over the bot. Push-to-talk makes "I
  want the floor" an explicit gesture, which is why Step 5 ships it first — it removes an
  entire class of half-duplex bug from the first version while leaving the server side
  identical, since the server can't tell a VAD-triggered `cancel` from a clicked one.

---

## Done when

An operator opens a bot's Test Agent link in **voice** mode, speaks into the browser, and
hears *that bot* answer in its own configured voice — with the measured **end-of-speech →
first audio byte p50/p95 shown in the tester UI and logged per turn**, a second turn that
remembers the first, and the socket refused outright on a bot with
`channels.voice.enabled = false`.

The latency number is part of the criterion, not a nice-to-have: a tester that works but
can't say whether we hold the §13 budget has delivered the demo and skipped the answer.
**Missing the budget is a pass for this phase** as long as the number is measured and
where it goes is explained — the point is to stop guessing. Only an unmeasured tester
fails.

---

## ⚠️ Blocked on external input — read before starting

| Need | State | Without it |
|---|---|---|
| **Deepgram API key** | `settings.deepgram_api_key` exists and **is read by nothing**. Not set locally. | No STT. The phase cannot be verified end to end. |
| **Cartesia API key** | `settings.cartesia_api_key` exists and IS used, but only by `app/tenancy/voice.py` (cloning). Set locally. | No TTS. |

Deepgram is the hard blocker — it's a paid key nobody has yet. **Confirm it exists before
Step 1**, or this phase stalls at exactly the point where it stops being verifiable, which
is the worst place to discover it. Everything up to Step 3 can be built against a fake STT
provider; nothing past it can be trusted without the real thing.

---

## Step 0 — close out 9.2's open items first

Small, and they're in the way. Voice inherits `sanitize.py`, so item 1 is not optional:
a restatement that reads as a stutter in chat is *far* worse spoken aloud, which is the
exact reason `RepeatSuppressor` was written in the first place.

1. **The cross-tool-hop restatement.** The model says the same thing twice around a tool
   call (~1 in 3 turns). `RepeatSuppressor` only guards the *first* sentence of a reply
   segment, so a restatement landing later in the segment is structurally invisible to it.
   Fix: compare each completed sentence in the new segment against every sentence already
   spoken this turn, not just the segment's opener. Keep the existing fail-safe posture —
   when unsure, speak it.
2. **Decide `prompt_augmentation`.** Both behaviours still ship. Pick one and delete the
   other (~20 lines), or confirm the toggle stays.
3. **Purge the leftover scratch tenants.** `flow-test`, and check `new-cringe-1` /
   `test-clinic` / `playmouth1` are still wanted.

---

## Architecture

```
browser mic (AudioWorklet, PCM16 @16k)
   ─ws─▶  /voice/live  ─ws─▶ Deepgram streaming STT
                        ────▶ stream_turn(channel="voice")     [UNCHANGED]
                        ─ws─▶ Cartesia streaming TTS
   ◀ws─  PCM16 @24k audio frames
```

### D1. WebSocket, not WebRTC

Railway's proxy is HTTP/TCP. No UDP ingress, no TURN — WebRTC would force a media server
and a second piece of infrastructure. A WebSocket carrying raw PCM needs neither. The
browser's own `echoCancellation: true` handles the bot-hears-itself problem that a media
server would otherwise be needed for.

### D2. Raw PCM end to end — no ffmpeg, no transcode, no apt layer

Deepgram accepts `linear16`; Cartesia emits `pcm_s16le`. Keeping both raw means the
Dockerfile doesn't change at all. Any resampling is browser-side in the AudioWorklet.

### D3. Provider seams from day one

```
app/voice/stt/base.py      SpeechToText protocol   → deepgram.py
app/voice/tts/base.py      TextToSpeech protocol   → cartesia.py
```

Chosen per tenant from `VoiceSettings` (which already carries `provider`, `voice_id`,
`model`, `speed`). This mirrors `BookingProvider` — the seam that later let Cal.com be
swapped from REST to MCP without touching a graph node. Cartesia voice cloning already
exists (`app/tenancy/voice.py`) and plugs straight in.

### D4. The brain is untouched

`stream_turn(channel="voice")` is called exactly as Vapi calls it. Reused unchanged:
`sanitize.py`, `acknowledge.py`, the `is_spoken` filter, `FIRST_TOKEN_BUDGET_MS`. **Not**
reused: `vapi_schema.py`, `webhooks.py`, `require_vapi_secret`, transcript reseeding —
those are Vapi's wire format, not voice's.

If this phase finds itself editing a graph node, something has gone wrong.

### D5. One event loop — the constraint most likely to bite

All relay work shares the process's single event loop, and this app is single-worker by
hard constraint. Any CPU-bound work on it stutters live audio for *every* tenant, not just
the one talking. Same discipline `app/rag/ingest.py` already applies with
`asyncio.to_thread`. Audio frames are small and frequent: a 20ms frame at 16kHz mono PCM16
is 640 bytes, so ~50 messages/second per direction per session.

### D6. The wire protocol — binary is audio, text is control

One socket, `GET /voice/live?token=<test link token>`, carrying two frame kinds. WebSocket
frames are *already* typed as binary or text, so the split costs nothing and avoids
base64'ing every 20ms frame into JSON — a 33% inflation plus a JSON parse ~50 times a
second per direction, for no benefit.

**The token is the first frame, not a query parameter — changed during implementation,
after checking rather than assuming.** The obvious design is `?token=...`, since a browser's
`new WebSocket()` cannot set an `Authorization` header (the `Sec-WebSocket-Protocol`
smuggling trick is worse — it lands the secret in a header the server must echo). But
uvicorn's access logger writes a WebSocket's full path **with its query string**
(`uvicorn/protocols/websockets/websockets_impl.py`, `get_path_with_query_string`), and
`app/logging_config.py` deliberately propagates `uvicorn.access` into the app's own
structured handler — so `?token=` would be copied verbatim into production logs on every
connect. The client therefore connects unauthenticated and sends
`{"type":"auth","token":"..."}` as its first frame; the socket is closed with `4401` if
that doesn't arrive within `voice_auth_timeout_seconds`. The cost is a short window where
an accepted socket has no identity, bounded by that timeout and by the per-IP open limit —
neither of which needs to know who the caller is.

**The same check turned up a pre-existing leak this phase fixed on the way past.**
`GET /test/{token}` carries the token *in its path*, which `app/middleware.py` has logged
at INFO since Phase 7 — so test-link tokens were already being written to logs on every
page load, WebSocket or no WebSocket. `redacted_path()` now rewrites `/test/<token>` to
`/test/<redacted>` in the access line. `/bot/{widget_key}` is deliberately left alone: a
widget key is a public identifier a client pastes into their own HTML, not a secret.

**Client → server**

| Frame | Payload | Meaning |
|---|---|---|
| binary | raw PCM16LE mono @16 kHz, ~20ms (640B) | microphone audio, only while the floor is held |
| text | `{"type":"start_utterance"}` | push-to-talk pressed: open STT, begin a turn |
| text | `{"type":"end_utterance"}` | released: no more audio for this turn, finalise STT |
| text | `{"type":"cancel"}` | abandon the turn in flight (see the barge-in note above) |
| text | `{"type":"text","text":"..."}` | type instead of speak — same turn path, no STT. Free, and it makes the whole orchestrator testable without an audio fixture |

**Server → client**

| Frame | Payload | Meaning |
|---|---|---|
| binary | raw PCM16LE mono @24 kHz | TTS audio for the current turn |
| text | `{"type":"ready","tenant":...,"sample_rate_in":16000,"sample_rate_out":24000,"turn_limit":N}` | handshake accepted; the client configures its worklet from this rather than hardcoding |
| text | `{"type":"state","state":"listening\|thinking\|speaking\|idle","turn":N}` | the state machine, mirrored for the UI |
| text | `{"type":"transcript","text":"...","final":bool,"turn":N}` | STT, interim and final |
| text | `{"type":"token","text":"...","turn":N}` | the reply as text, alongside the audio |
| text | `{"type":"metrics","turn":N,"stt_final_ms":...,"first_token_ms":...,"first_audio_ms":...}` | per-turn latency, `first_audio_ms` being the §13 number |
| text | `{"type":"error","message":"..."}` | recoverable: the turn died, the socket lives |
| text | `{"type":"closing","reason":"..."}` | terminal: session cap, idle timeout, disabled channel |

**Every message carries `turn`**, and that is load-bearing rather than decorative: after a
`cancel`, TTS frames already in flight can still arrive, and a client with no way to tell
which turn a frame belongs to will play the audio of a turn the user just abandoned. The
client discards any frame whose turn is not the current one; binary frames inherit the turn
from the most recent `state` message, which is why `state` is always sent *before* the first
audio frame of a turn, never after.

`stream_turn(channel="voice")` is called with exactly the arguments Vapi's adapter uses.
No new brain-facing vocabulary: `token`/`acknowledgement` become audio (`BrainEvent.is_spoken`
is already the filter), `tool_start`/`tool_result` are logged only — the same rule
`vapi_llm.py` follows, for the same reason (CLAUDE.md: tool events must never become audio).

### D7. Nothing is persisted, and that is a decision

`/voice/live` writes **no rows** and this phase adds **no migration**. Three separate
reasons, because "we didn't get to it" and "it doesn't belong" look identical in a schema:

1. **Conversation memory already works without it.** The thread lives in the LangGraph
   checkpointer, the same path chat uses, so "the second turn remembers the first" needs
   no storage of ours. Thread id is `voice:<uuid4>` — it can't collide with a Vapi call id
   (the voice thread key today) or a widget session id.
2. **`chat_sessions`/`chat_messages` are the widget's, and deliberately so.** Phase 5 writes
   them only for `mode="widget"` callers, because a `ChatSession` row is guaranteed to exist
   from the handshake before any message can reference it. A test-link socket has no widget
   session and no handshake of that shape; borrowing the table would mean inventing the
   parent row.
3. **`calls` is Vapi's end-of-call report table, and Phase 8's analytics views count it.**
   Writing relay sessions there would inflate every tenant's call metrics with operator
   tests — corrupting the dashboard to record something nobody asked to see.

Latency metrics are logged structured (`app/middleware.py`'s request-id correlation) and
returned live in `metrics`; they are not stored. **When voice stops being a tester and
becomes a channel a customer actually reaches, that is when a `voice_sessions` table earns
its migration** — and it should mirror `chat_sessions`, not `calls`.

---

## Cost, and why spend containment is in this phase rather than after it

Per §14's verified rates, a relay minute is **STT ~$0.007 + TTS ~$0.020 + LLM ~$0.003 ≈
$0.03/min** — against ~$0.09–0.15 for a Vapi phone minute, because the relay drops Vapi's
$0.05/min platform fee and telephony entirely. That gap is the second, quieter reason this
phase is worth building: it is the cheapest voice path we have, and the only one we can
measure.

It is also metered per minute behind a URL, which is the same spend hole `plans/phase10.md`
item 13 flags for the avatar endpoint. Four caps, all in Step 2, none deferred:

- `voice_max_session_seconds` (default 300) — the server closes with `closing`, not silence.
- `voice_max_turns_per_session` (default 30).
- `voice_idle_timeout_seconds` (default 60) — an abandoned open tab costs nothing.
- a per-IP socket-open ceiling on `app/channels/ratelimit.py`'s existing `_hit`, in its own
  `voice-ip` scope (the `test-session-ip` precedent: separate budgets, so an operator's
  tester can't eat real visitor traffic's allowance or vice versa).

The STT socket is opened **per utterance and closed at the end of the turn**, not held for
the session — an idle connected tab must not bill Deepgram for silence.

---

## Steps

**1. Provider protocols + fakes** — `app/voice/stt/base.py`, `tts/base.py`, plus in-repo
fakes (STT returns scripted transcripts, TTS returns silence of the right length). Every
step below is testable offline against these; the real providers are swapped in at Step 4.
Guarded by `tests/test_voice_providers.py`.

**2. `/voice/live` WebSocket** (`app/channels/voice_live.py`) — authenticated by the *same*
signed test-link token (`mode: "voice"`), gated by `channels.voice.enabled`. Owns the
session state machine: listening → thinking → speaking. Rate-limited per token, and
capped per the spend section above. Guarded by `tests/test_voice_live_socket.py` (auth,
mode mismatch both directions, disabled channel, unknown tenant, the caps).

**3. Turn orchestration** — STT endpointing fires → `stream_turn(channel="voice")` →
sentence-chunk the token stream → TTS per chunk → frames out. Do **not** wait for the full
reply before speaking: chunk on sentence boundaries so first audio starts on the first
sentence. This is where the §13 budget is won or lost. Reuse `app/brain/sanitize.py`'s
`_SENTENCE_END` rather than writing a second sentence splitter — two that disagree is how
the `RepeatSuppressor` bugs happened. Guarded by `tests/test_voice_turn.py` (chunk
boundaries, cancel mid-speech, client disconnect mid-turn, no orphaned tasks after
teardown, tool events never reaching audio).

**4. Real Deepgram + Cartesia adapters** — needs the keys above. Lazily imported and
self-degrading on `ImportError`, the `app/mcp/client.py` pattern: a missing `websockets`
extra must cost voice, never the app. Offline tests cover request/response *shape* only
(`tests/test_voice_deepgram.py`, `tests/test_voice_cartesia.py`) — they prove our framing,
never the vendor's, the same honesty caveat Part A's Cal.com MCP schemas carried until a
live call closed it.

**5. Browser client** — an AudioWorklet in the existing Test Agent page. `/test/{token}`
with `mode: "voice"` renders a mic UI instead of the chat widget; the two share the page
shell (`_hosted_widget_page`) but not the transport. Push-to-talk first, VAD second —
push-to-talk removes an entire class of bug from the first version.

**6. Latency instrumentation** — measure end-of-speech → first audio byte, log p50/p95 per
turn, and surface it in the tester UI. The number is the deliverable; without it we've
built a demo, not an answer to "does our own path hold the budget?" Guarded by
`tests/test_voice_metrics.py` (the clock starts at `end_utterance`, not at socket open).

**7. Admin** — the Test Agent button gains a chat/voice choice, greyed per
`channels.voice.enabled` (9.1 already built that gating).

### Files this phase adds

```
app/voice/protocol.py        the D6 vocabulary — one source of truth for both ends
app/voice/audio.py           PCM framing helpers (no transcode, per D2)
app/voice/stt/base.py        SpeechToText protocol + Transcript
app/voice/stt/fake.py        scripted, for every offline test
app/voice/stt/deepgram.py    real (Step 4)
app/voice/tts/base.py        TextToSpeech protocol
app/voice/tts/fake.py        silence of the right length
app/voice/tts/cartesia.py    real (Step 4)
app/voice/providers.py       stt_for()/tts_for() + test overrides, mirroring app/tools/providers.py
app/voice/session.py         the turn orchestrator and state machine
app/channels/voice_live.py   the WebSocket route (transport only — no orchestration)
```

`app/voice/` is a new top-level package rather than more files under `app/channels/`,
deliberately: `channels/` is adapters, and everything above except `voice_live.py` is
provider-facing infrastructure the adapter merely drives — the same split `app/tools/`
and `app/tools/booking/` already have.

---

## Verification

**Offline** — fake providers throughout: the state machine (barge-in mid-speech, silence
timeout, client disconnect mid-turn), `channels.voice.enabled=false` refusing the socket, a
`mode: "chat"` token refused by `/voice/live` and vice versa, cross-tenant isolation, and
teardown leaving no orphaned tasks (the `aclose_calcom_mcp_sessions` lesson).

**Live** — real keys, real browser, one real conversation. Then:

1. Speak → the bot answers audibly, in the tenant's configured voice.
2. **Report p50/p95 end-of-speech → first audio.** Pass = inside 600–800ms. If it isn't,
   say so plainly and where the time goes — that's a finding, not a failure.
3. Two turns: the second must remember the first (same checkpointer path as chat).
4. `channels.voice.enabled=false` → the socket is refused.
5. Compare the same prompt on voice vs. chat: `${ui_rule}` must NOT appear on voice, and no
   button/card tool may be bound.
6. **Click through it in a real browser.** Non-negotiable this time — 9.1/9.2 shipped three
   UI bugs (`/undefined` redirect, comma-eating list field, Danger Zone reading draft
   status) that every test passed and one minute of clicking caught.

---

## Status — built 13 Sep 2026

**Steps 1–3, 5, 6 and 7 are done; Step 4's adapters are written but unverified.**
65 tests (`test_voice_live_socket.py` 17, `test_voice_turn.py` 14, `test_voice_metrics.py` 7,
`test_voice_providers.py` 10, `test_voice_deepgram.py` 8, `test_voice_cartesia.py` 8, plus
two in `test_logging.py`); suite 1064 → 1129, all green, ruff clean.

**The fake providers are the default, not test scaffolding.** `VOICE_STT_PROVIDER=fake` /
`VOICE_TTS_PROVIDER=fake` ship as the defaults so a box with no Deepgram key still gets a
tester with a real state machine, the real brain and real latency numbers — canned
transcripts and silent audio of the right length. That is what let Steps 1–3, 5 and 6 be
built and verified end to end while the phase's stated blocker is still open. Naming a real
provider without its key fails the socket loudly rather than degrading to silence, which an
operator would read as the *bot* being broken.

### The measurement — the deliverable, and it misses the budget

Live, against the real stack (real Gemini, `store: supabase`, `checkpointer: postgres`),
three consecutive turns on one socket, typed rather than spoken (so no STT leg):

| | first token | first audio | total |
|---|---|---|---|
| turn 1 | 1061ms | **1107ms** | 2152ms |
| turn 2 | 2006ms | **2062ms** | 2539ms |
| turn 3 | 1030ms | **1089ms** | 2155ms |

**p50 1107ms, p95 2062ms — outside the §13 600–800ms budget.** Per this phase's own "Done
when", that is a pass: the number is measured and where it goes is explainable.

**Where it goes: almost entirely the model.** `first_audio - first_token` is 46ms, 56ms and
59ms — that gap *is* the relay (chunk detection, TTS dispatch, framing, the socket), and it
is not the problem. The 1.0–2.0s to first token is Gemini's own time-to-first-token, over
the public API, from a dev box in Karachi to a US-anchored endpoint, with the full system
prompt (hours, knowledge, UI rules) in front of it. Two things would move it and neither is
in this phase: running from the deployed region (Railway `us-east4`, co-located with the
model and the database) and a faster model. A real TTS leg will *add* its own
time-to-first-byte on top, so the honest reading is that the budget is model-bound before
it is ever relay-bound.

**A cold first turn measured 17.8s, and that is a separate finding.** The first turn in a
fresh process paid an MCP connect failure to `hotel-mzv`'s configured long-tail server
(unreachable from this box — the documented degrade-to-`[]` path, with the retry cost that
implies) plus the cold Cal.com schedule/OAuth path Phase 9.4 already records. Nothing to do
with the relay, but worth knowing before anyone reads a first-turn number as representative.

### Still owed

- ~~**Step 4.**~~ **Both vendor legs are live-verified (13 Sep 2026).** The Deepgram key
  arrived the same day and the STT leg was proven **without a microphone**, by a closed
  loop: Cartesia synthesized *"Do you have a room available tomorrow night?"* at the
  socket's own 16kHz input rate, those frames were streamed into `/voice/live` in real time
  exactly as a browser would, and **Deepgram returned the sentence verbatim** — interim
  result included. The bot then answered with real Cal.com slots and spoke back 434KB of
  real Cartesia audio. So `app/voice/stt/deepgram.py`'s connect query, `Token` auth header,
  `Results`/`Metadata` framing and `CloseStream` flush are all confirmed against the real
  service, not assumed.

  Cartesia was verified the same day (235KB / 4.9s of genuine speech, peak amplitude 32767,
  100% non-silent, no `TtsError`).

  **Measured legs, warm, on a turn that also calls Cal.com:** Deepgram finalization
  **299ms–1.0s** after end-of-speech, model + tool round trip ~1.9s, Cartesia **~350ms**,
  our relay ~50ms — first audio at 2.6–3.3s. A turn with no tool call was ~1.3s. All from a
  dev box in Karachi to US-anchored endpoints, with three separate cross-continental legs;
  the deployed region is the obvious lever and is the first thing worth re-measuring.

- **A real browser click-through.** The page is served and correct as HTML (verified over
  real HTTP), and the socket is proven by a real client — but no microphone has ever
  reached it. The AudioWorklet, the `getUserMedia` permission flow and the playback
  scheduling are the least-tested code in this phase, and 9.1/9.2 shipped three UI bugs
  that every test passed. **Non-negotiable before calling this done.**
- **Step 0's leftovers** (`prompt_augmentation`, the scratch tenants) are untouched; item 1
  was already fixed in 9.2's own follow-up.

---

## Risks

| Risk | Why it matters | Mitigation |
|---|---|---|
| **No Deepgram key** | Blocks Steps 4–6 entirely | Confirm before Step 1 |
| **Latency misses §13** | The whole justification | Measure at Step 6, before the UI is polished; if the relay can't hold it, that's worth knowing early and cheaply |
| **Event-loop stutter** | Degrades *every* tenant, not just the speaker | No CPU work on the loop; load-test with concurrent sessions |
| **Scope creep into replacing Vapi** | Vapi handles PSTN, carriers, telephony edge cases we don't | This is a tester and a seam. PSTN stays with Vapi until there's a reason it shouldn't |
| **Browser audio is fiddly** | AudioWorklet, sample rates, autoplay policy, permissions | Push-to-talk first; one browser (Chrome) first |
