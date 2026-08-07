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
- **Barge-in / interruption handling** beyond stopping TTS when speech is detected. Full
  duplex turn-taking is its own problem; get one clean turn first.

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

---

## Steps

**1. Provider protocols + fakes** — `app/voice/stt/base.py`, `tts/base.py`, plus in-repo
fakes (STT returns scripted transcripts, TTS returns silence of the right length). Every
step below is testable offline against these; the real providers are swapped in at Step 4.

**2. `/voice/live` WebSocket** (`app/channels/voice_live.py`) — authenticated by the *same*
signed test-link token (`mode: "voice"`), gated by `channels.voice.enabled`. Owns the
session state machine: listening → thinking → speaking. Rate-limited per token.

**3. Turn orchestration** — STT endpointing fires → `stream_turn(channel="voice")` →
sentence-chunk the token stream → TTS per chunk → frames out. Do **not** wait for the full
reply before speaking: chunk on sentence boundaries so first audio starts on the first
sentence. This is where the §13 budget is won or lost.

**4. Real Deepgram + Cartesia adapters** — needs the keys above.

**5. Browser client** — an AudioWorklet in the existing Test Agent page. `/test/{token}`
with `mode: "voice"` renders a mic UI instead of the chat widget; the two share the page
shell (`_hosted_widget_page`) but not the transport. Push-to-talk first, VAD second —
push-to-talk removes an entire class of bug from the first version.

**6. Latency instrumentation** — measure end-of-speech → first audio byte, log p50/p95 per
turn, and surface it in the tester UI. The number is the deliverable; without it we've
built a demo, not an answer to "does our own path hold the budget?"

**7. Admin** — the Test Agent button gains a chat/voice choice, greyed per
`channels.voice.enabled` (9.1 already built that gating).

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

## Risks

| Risk | Why it matters | Mitigation |
|---|---|---|
| **No Deepgram key** | Blocks Steps 4–6 entirely | Confirm before Step 1 |
| **Latency misses §13** | The whole justification | Measure at Step 6, before the UI is polished; if the relay can't hold it, that's worth knowing early and cheaply |
| **Event-loop stutter** | Degrades *every* tenant, not just the speaker | No CPU work on the loop; load-test with concurrent sessions |
| **Scope creep into replacing Vapi** | Vapi handles PSTN, carriers, telephony edge cases we don't | This is a tester and a seam. PSTN stays with Vapi until there's a reason it shouldn't |
| **Browser audio is fiddly** | AudioWorklet, sample rates, autoplay policy, permissions | Push-to-talk first; one browser (Chrome) first |
