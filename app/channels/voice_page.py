"""The voice tester's browser page (Phase 9.3, Step 5).

Server-rendered HTML with inline JS, deliberately — not a third build
toolchain beside `widget/` and `admin/`. There is nothing to embed here: this
page is never pasted into a client's site, it is only ever served by us at
`/test/{token}`, so it has no frozen `<script>` contract to keep and no
bundle to version. `plans/phase10.md` item 13 already made the "ride an
existing toolchain rather than add one" call for the avatar; a page with no
dependencies doesn't even need that much.

The AudioWorklet is loaded from a Blob URL for the same reason: an
`addModule()` needs a *URL*, and inlining one avoids serving a second static
file that would then need its own cache-header decision (see the `/widget.js`
`immutable` gotcha, which cost a live debugging session).

Two sample rates, two AudioContexts: capture is pinned to 16kHz and playback
to 24kHz, both taken from the `ready` handshake rather than hardcoded, so
changing `app/voice/audio.py` never means editing this file.
"""

from __future__ import annotations

import html
import json

_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>__TITLE__</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root { color-scheme: dark; }
    html, body { background: #000; }
    body {
      font-family: system-ui, -apple-system, sans-serif;
      margin: 0; padding: 2rem; min-height: 100vh; box-sizing: border-box;
      color: #e2e8f0;
    }
    h1 { font-size: 1.25rem; margin: 0 0 .35rem; color: __HEADING_COLOR__; }
    .note { color: __NOTE_COLOR__; max-width: 44rem; line-height: 1.5; margin: 0 0 1.5rem; }
    .panel {
      max-width: 44rem; background: #0b0f17; border: 1px solid #1e293b;
      border-radius: 14px; padding: 1.25rem;
    }
    .row { display: flex; gap: .75rem; align-items: center; flex-wrap: wrap; }
    button {
      font: inherit; border-radius: 10px; border: 1px solid #334155;
      background: #1e293b; color: #e2e8f0; padding: .6rem 1rem; cursor: pointer;
    }
    button:disabled { opacity: .45; cursor: default; }
    #talk { background: #1d4ed8; border-color: #2563eb; font-weight: 600; min-width: 12rem; }
    #talk.live { background: #b91c1c; border-color: #dc2626; }
    #state { font-size: .85rem; color: #94a3b8; margin-left: auto; }
    .dot {
      display: inline-block; width: .55rem; height: .55rem; border-radius: 50%;
      background: #475569; margin-right: .4rem; vertical-align: middle;
    }
    .dot.listening { background: #ef4444; }
    .dot.thinking { background: #f59e0b; }
    .dot.speaking { background: #22c55e; }
    #log {
      margin-top: 1rem; border-top: 1px solid #1e293b; padding-top: 1rem;
      max-height: 22rem; overflow-y: auto; line-height: 1.5;
    }
    .mode select {
      font: inherit; font-size: .85rem; padding: .45rem .5rem; border-radius: 10px;
      border: 1px solid #334155; background: #0f172a; color: #cbd5e1;
    }
    .hint { font-size: .8rem; color: #64748b; margin: .75rem 0 0; line-height: 1.45; }
    .turn { margin-bottom: .9rem; }
    .said { color: #93c5fd; }
    .said.interim { opacity: .55; font-style: italic; }
    .reply { color: #e2e8f0; }
    .err { color: #fca5a5; }
    .metrics { font-size: .8rem; color: #64748b; margin-top: .25rem; }
    .turn-err { font-size: .8rem; margin-top: .25rem; }
    .budget-ok { color: #4ade80; }
    .budget-over { color: #fbbf24; }
    #summary {
      margin-top: 1rem; font-size: .85rem; color: #94a3b8;
      border-top: 1px solid #1e293b; padding-top: .75rem;
    }
    input[type=text] {
      flex: 1; min-width: 12rem; font: inherit; padding: .55rem .7rem;
      border-radius: 10px; border: 1px solid #334155; background: #0f172a; color: #e2e8f0;
    }
  </style>
</head>
<body>
  <h1>__HEADING__</h1>
  <p class="note">__NOTE__</p>
  <div class="panel">
    <div class="row">
      <button id="talk" disabled>Connecting…</button>
      <button id="stop" disabled title="Stop the bot talking (the mic stays on)">Hush</button>
      <label class="mode" title="How a pause mid-sentence is treated">
        <select id="mode">
          <option value="continuation">Wait for me to finish</option>
          <option value="instant">Send each sentence instantly</option>
        </select>
      </label>
      <span id="state"><span class="dot"></span><span id="stateText">offline</span></span>
    </div>
    <p class="hint">
      Turn the mic on and just talk — it stays on for the whole conversation, so there is
      nothing to press between questions. <strong>Wait for me to finish</strong> lets you
      pause mid-sentence without the bot answering half of it;
      <strong>instantly</strong> replies the moment you stop, which is faster and will cut
      you off if you think out loud. Speaking over the bot stops it on the first word.
      Headphones are worth it: an open mic can otherwise hear the bot through your
      speakers.
    </p>
    <div class="row" style="margin-top:.75rem">
      <input type="text" id="typed" placeholder="…or type a message instead" disabled />
      <button id="send" disabled>Send</button>
    </div>
    <div id="log"></div>
    <div id="summary">No turns yet.</div>
  </div>
<script>
const CONFIG = __CONFIG__;
const WS_SCHEME = location.protocol === "https:" ? "wss://" : "ws://";
const WS_URL = WS_SCHEME + location.host + "/voice/live";

const els = {
  talk: document.getElementById("talk"),
  stop: document.getElementById("stop"),
  dot: document.querySelector("#state .dot"),
  stateText: document.getElementById("stateText"),
  log: document.getElementById("log"),
  summary: document.getElementById("summary"),
  typed: document.getElementById("typed"),
  send: document.getElementById("send"),
  mode: document.getElementById("mode"),
};

let socket = null;
let ready = null;
let holding = false;
let currentTurn = 0;
let turnNodes = {};
const firstAudioSamples = [];

// --- playback -------------------------------------------------------------
// One AudioContext for output, at the rate the handshake declared. Chunks are
// scheduled back to back against a moving playhead rather than played on
// arrival, or every network jitter becomes an audible gap.
let outCtx = null;
let playhead = 0;

function stopPlayback() {
  if (outCtx) { outCtx.close(); outCtx = null; }
  playhead = 0;
}

function playPcm(buffer) {
  if (!outCtx) {
    outCtx = new AudioContext({ sampleRate: ready.sample_rate_out });
    playhead = outCtx.currentTime;
  }
  // Defensive: `new Int16Array` throws on an odd byte length, which would
  // kill this frame inside onmessage. The server keeps the stream
  // sample-aligned (app/voice/session.py::_speak) — this is the second line
  // of that defence, not a substitute for it.
  const usable = buffer.byteLength - (buffer.byteLength % 2);
  if (!usable) return;
  const pcm = new Int16Array(buffer, 0, usable / 2);
  if (!pcm.length) return;
  const audio = outCtx.createBuffer(1, pcm.length, ready.sample_rate_out);
  const channel = audio.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 32768;
  const source = outCtx.createBufferSource();
  source.buffer = audio;
  source.connect(outCtx.destination);
  const at = Math.max(outCtx.currentTime, playhead);
  source.start(at);
  playhead = at + audio.duration;
}

// --- capture --------------------------------------------------------------
// The worklet lives in a Blob so this page stays a single file. It emits
// frame-sized Int16 buffers; the resampling to 16kHz is the AudioContext's.
const WORKLET = `
class PcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.frame = options.processorOptions.frameSamples;
    this.buffer = new Int16Array(this.frame);
    this.filled = 0;
  }
  process(inputs) {
    const input = inputs[0][0];
    if (!input) return true;
    for (let i = 0; i < input.length; i++) {
      const clamped = Math.max(-1, Math.min(1, input[i]));
      this.buffer[this.filled++] = clamped < 0 ? clamped * 32768 : clamped * 32767;
      if (this.filled === this.frame) {
        const out = this.buffer.slice();
        this.port.postMessage(out.buffer, [out.buffer]);
        this.filled = 0;
      }
    }
    return true;
  }
}
registerProcessor("pcm-capture", PcmCapture);
`;

let inCtx = null;
let micNode = null;

async function startMic() {
  if (micNode) return;
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
  });
  inCtx = new AudioContext({ sampleRate: ready.sample_rate_in });
  const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
  await inCtx.audioWorklet.addModule(url);
  URL.revokeObjectURL(url);
  const frameSamples = Math.round(ready.sample_rate_in * ready.frame_ms / 1000);
  micNode = new AudioWorkletNode(inCtx, "pcm-capture", {
    processorOptions: { frameSamples },
  });
  micNode.port.onmessage = (event) => {
    if (holding && socket && socket.readyState === WebSocket.OPEN) socket.send(event.data);
  };
  inCtx.createMediaStreamSource(stream).connect(micNode);
  // A zero-gain sink: some browsers stop pulling from a worklet that isn't
  // connected to anything, which silently produces no frames at all.
  const sink = inCtx.createGain();
  sink.gain.value = 0;
  micNode.connect(sink).connect(inCtx.destination);
}

// --- transcript ----------------------------------------------------------
function turnNode(turn) {
  if (!turnNodes[turn]) {
    const wrap = document.createElement("div");
    wrap.className = "turn";
    wrap.innerHTML =
      '<div class="said"></div><div class="reply"></div>' +
      '<div class="turn-err"></div><div class="metrics"></div>';
    els.log.appendChild(wrap);
    turnNodes[turn] = wrap;
  }
  els.log.scrollTop = els.log.scrollHeight;
  return turnNodes[turn];
}

function setState(value) {
  els.dot.className = "dot " + value;
  els.stateText.textContent = value;
  els.stop.disabled = (value === "idle");
}

function renderSummary() {
  if (!firstAudioSamples.length) return;
  const sorted = [...firstAudioSamples].sort((a, b) => a - b);
  const at = (f) => sorted[Math.max(0, Math.ceil(f * sorted.length) - 1)];
  const p50 = Math.round(at(0.5)), p95 = Math.round(at(0.95));
  const verdict = p50 <= 800 ? "inside" : "over";
  els.summary.innerHTML =
    `<strong>${sorted.length} turn(s)</strong> · end-of-speech to first audio — ` +
    `p50 <strong>${p50}ms</strong>, p95 <strong>${p95}ms</strong> · ` +
    `<span class="${p50 <= 800 ? "budget-ok" : "budget-over"}">` +
    `${verdict} the 600-800ms budget</span>`;
}

const ms = (value) => (value == null ? "-" : Math.round(value) + "ms");
const inBudget = (value) => value != null && value <= 800;

function handle(message) {
  switch (message.type) {
    case "ready":
      ready = message;
      els.talk.disabled = false;
      els.talk.textContent = "🎤 Mic off — click to talk";
      els.typed.disabled = false;
      els.send.disabled = false;
      if (message.utterance_mode) els.mode.value = message.utterance_mode;
      els.summary.textContent =
        `Connected to ${message.tenant_name}` +
        ` · stt: ${message.stt_provider} · tts: ${message.tts_provider}`;
      setState("idle");
      break;
    case "state":
      // Binary frames carry no turn of their own and inherit this one, which
      // is why `state` always precedes a turn's first audio frame.
      if (message.turn !== currentTurn) { currentTurn = message.turn; stopPlayback(); }
      setState(message.state);
      break;
    case "transcript": {
      const node = turnNode(message.turn);
      const said = node.querySelector(".said");
      // Finals accumulate; only the trailing interim is replaced. A pause
      // mid-sentence produces two finals for one question, and overwriting
      // made the first half disappear from the screen while the bot had
      // heard all of it — which read as the bot losing track.
      if (message.final) {
        said.dataset.final = ((said.dataset.final || "") + " " + message.text).trim();
      }
      const tail = message.final ? "" : " " + message.text;
      said.textContent = "you: " + ((said.dataset.final || "") + tail).trim();
      said.className = "said" + (message.final ? "" : " interim");
      break;
    }
    case "token": {
      const reply = turnNode(message.turn).querySelector(".reply");
      reply.textContent = (reply.textContent || "bot: ") + message.text;
      break;
    }
    case "metrics": {
      const node = turnNode(message.turn).querySelector(".metrics");
      const first = message.first_audio_ms;
      if (first != null) { firstAudioSamples.push(first); renderSummary(); }
      node.innerHTML =
        (message.interrupted
          ? '<span class="err">interrupted — the answer was still coming</span> · '
          : "") +
        `first audio <span class="${inBudget(first) ? "budget-ok" : "budget-over"}">` +
        `${first == null ? "none" : ms(first)}</span>` +
        ` · first token ${ms(message.first_token_ms)}` +
        ` · transcript ${ms(message.stt_final_ms)}` +
        ` · total ${ms(message.total_ms)}`;
      break;
    }
    case "error": {
      // Its own line, not the metrics line. They shared one, and metrics
      // always arrive last — so a turn that died mid-answer overwrote its
      // own explanation and looked like a bot that simply stopped talking.
      const node = turnNode(message.turn || currentTurn).querySelector(".turn-err");
      node.innerHTML = '<span class="err">' + message.message + "</span>";
      break;
    }
    case "closing":
      els.summary.innerHTML += `<br><span class="err">closed: ${message.reason}</span>`;
      els.talk.disabled = true;
      els.typed.disabled = true;
      els.send.disabled = true;
      setState("idle");
      break;
  }
}

function connect() {
  socket = new WebSocket(WS_URL);
  socket.binaryType = "arraybuffer";
  socket.onopen = () => socket.send(JSON.stringify({ type: "auth", token: CONFIG.token }));
  socket.onmessage = (event) => {
    if (typeof event.data === "string") handle(JSON.parse(event.data));
    else if (ready) playPcm(event.data);
  };
  socket.onclose = () => {
    setState("idle");
    els.talk.disabled = true;
    els.talk.textContent = "Disconnected";
    els.typed.disabled = true;
    els.send.disabled = true;
    stopPlayback();
  };
}

function send(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
}

// A mic switch, not a talk button. On means on: it stays open across the
// whole conversation, and the server's speech recognition decides where each
// sentence ends (Deepgram's own endpointing) rather than making the listener
// announce it. Nothing here times anything or measures silence — a browser
// guessing at that would be a second, worse opinion about the same audio.
async function micOn() {
  try {
    await startMic();
  } catch (err) {
    els.summary.innerHTML = '<span class="err">microphone blocked: ' + err + "</span>";
    return;
  }
  if (inCtx && inCtx.state === "suspended") await inCtx.resume();
  holding = true;
  els.talk.classList.add("live");
  els.talk.textContent = "🔴 Mic on — click to mute";
  send({ type: "mic_on" });
}

function micOff() {
  if (!holding) return;
  holding = false;
  els.talk.classList.remove("live");
  els.talk.textContent = "🎤 Mic off — click to talk";
  send({ type: "mic_off" });
}

els.talk.addEventListener("click", () => {
  if (holding) micOff();
  else micOn();
});
// Space toggles it too — but never while typing in the text box, where a
// space is just a space.
document.addEventListener("keydown", (event) => {
  if (event.code === "Space" && event.target !== els.typed && !els.talk.disabled) {
    event.preventDefault();
    if (holding) micOff();
    else micOn();
  }
});
els.stop.addEventListener("click", () => { stopPlayback(); send({ type: "cancel" }); });
els.mode.addEventListener("change", () => send({ type: "mode", value: els.mode.value }));
els.send.addEventListener("click", () => {
  const text = els.typed.value.trim();
  if (!text) return;
  els.typed.value = "";
  stopPlayback();
  send({ type: "text", text });
});
els.typed.addEventListener("keydown", (event) => {
  if (event.key === "Enter") els.send.click();
});

connect();
</script>
</body>
</html>
"""


def render_voice_tester_page(tenant_name: str, token: str, *, variant: str) -> str:
    """The voice equivalent of `_test_agent_page`.

    Keeps the draft/live accent the chat tester uses — amber still reads as
    "unpublished" against black, and losing that signal on a second surface
    would be worse than any styling win.
    """
    is_draft = variant == "draft"
    label = "Voice draft preview" if is_draft else "Voice tester"
    note = (
        "Speaking to the unpublished draft — it is re-read on every turn, so further "
        "edits show up live in this tab."
        if is_draft
        else "A private, signed preview — this link isn't discoverable and expires on its own. "
        "Hold the button, speak, release. Latency is measured end-of-speech to first audio."
    )
    heading = f"{label} — {tenant_name}"
    return (
        _PAGE.replace("__TITLE__", html.escape(heading))
        .replace("__HEADING__", html.escape(heading))
        .replace("__NOTE__", html.escape(note))
        .replace("__HEADING_COLOR__", "#fbbf24" if is_draft else "#e2e8f0")
        .replace("__NOTE_COLOR__", "#fcd34d" if is_draft else "#94a3b8")
        # `json.dumps`, never string interpolation: the token goes into a
        # JS literal, and quoting it by hand is how an XSS gets written.
        .replace("__CONFIG__", json.dumps({"token": token, "variant": variant}))
    )
