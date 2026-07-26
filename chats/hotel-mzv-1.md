# AI Receptionist — Context Handoff (session: hotel-mzv-1)

> **Purpose of this file:** give a fresh chat window the complete context to continue work
> without re-reading the whole codebase. Last updated end of session `hotel-mzv-1`.
> Canonical spec is `AI-Receptionist-Build-Plan.md`; conventions in `CLAUDE.md`.

---

## 1. What this project is

A **multi-tenant AI receptionist**: one LangGraph "brain" that answers both **phone**
(via Vapi Custom-LLM mode) and **web chat** for many businesses at once. It books jobs,
sends confirmations, and escalates emergencies. **One brain, two channels** — all logic
lives in the graph; Vapi and the chat endpoint are thin adapters.

Single deployable service: FastAPI + LangGraph (Python). Everything else is managed SaaS
called over the network (LLM, Vapi, Cartesia, Deepgram, later Supabase/Twilio/Google).

---

## 2. Current status snapshot

| | |
|---|---|
| **LLM in use** | Google **Gemini `gemini-3.1-flash-lite`** (`LLM_PROVIDER=google` in `.env`) |
| **Also supported** | Groq (Llama 3.3 70B), any OpenAI-compatible endpoint (DeepSeek/GLM/Qwen/OpenRouter/Ollama) — one env var to switch |
| **Pilot tenant** | `hotel-mzv` — **named "Hotel_MZV" but still ELECTRICIAN-shaped** (services = panel upgrades, EV chargers). Not yet converted to real hotel services. |
| **Vapi assistant** | `bbc30129-3dc2-45c8-a1e9-56bc7d606cf9` ("Hotel_MZV receptionist"), web-call ready |
| **Public URL (dev)** | reserved ngrok domain `https://ranked-wielder-clarify.ngrok-free.dev` |
| **Tests** | **187 passing, ruff clean**, run fully offline (scripted model, hermetic settings) |
| **Booking storage** | **In-memory only (RAM)** — `app/db/memory_store.py`. Wiped on restart. No DB yet. |
| **Runs on** | Windows dev box, `.venv` (Python 3.12). Not yet deployed. |

---

## 3. Phase completion (plan §15 — phases 0–8, **there is no phase 9–16**)

| Phase | Status | Notes |
|---|---|---|
| **0 — Prereqs / keys** | ✅ Done | Groq, Vapi, Cartesia, Supabase, Google keys in `.env`. Twilio not set (optional). |
| **1 — Brain skeleton (single-tenant, stubbed tools)** | ✅ Done | Graph + 5 native tools + terminal chat. Books a fake job end-to-end. Verified live. |
| **2 — Voice via Vapi Custom-LLM** | ✅ Done | OpenAI-compatible `/chat/completions` SSE shim, webhooks (`calls` records), `scripts/provision_vapi.py`. Verified via **web call**; real-number attach available (`--attach-number`). |
| **3 — Real critical-path tools** | ❌ Not started | Google Calendar, Twilio SMS, Vapi warm transfer are **stubs**. **Blocked on booking-provider decision** (default = Google Calendar). |
| **4 — Multi-tenancy** | 🟡 Partial | ✅ tenant resolver, cached config loader, per-tenant Vapi provisioning, per-tenant voice, isolation (tested). ❌ Supabase+RLS persistence, voice cloning, encrypted secrets vault. Data is JSON files, not a DB. |
| **5 — Chatbot channel** | 🟡 Partial | ✅ `POST /chat` SSE endpoint works. ❌ embeddable JS widget (only a README), WhatsApp. |
| **6 — MCP layer** | ❌ Not started | `app/mcp/client.py` returns `[]`. |
| **7 — Deploy + harden** | ❌ Not started | `infra/Dockerfile` exists (copies `content/`), not deployed. Latency is instrumented but not region-tested. No A2P 10DLC. |
| **8 — Avatar** | ❌ Not started | Optional (Tavus/Simli). |

**Tally:** 3 fully done (0,1,2) · 2 partial (4,5) · 4 not started (3,6,7,8).

### Significant work done BEYOND the plan
- **Multi-provider LLM** with a `google` provider (Gemini) + OpenAI-compatible escape hatch.
- **`scripts/check_model.py`** — preflights that a model exists AND supports tool calling
  (the booking flow is entirely tool calls; a non-tool model fails *silently*).
- **Cost/latency instrumentation** — per-turn LLM request counter, time-to-first-token log
  vs a 400ms budget.
- **Robustness for weak tool-callers** (`app/brain/sanitize.py` + retry in `reason.py`):
  strips/promotes tool calls leaked as text; suppresses re-spoken acknowledgements;
  retries malformed calls once but NOT quota/auth errors.
- **`content/` folder** — all user-editable content consolidated (see §6).
- **Hermetic test suite** — ignores the dev box's `.env`/env vars.

---

## 4. How it works (architecture)

Graph (`app/brain/graph.py`):
```
START → resolve_tenant → emergency_check → reason ⇄ tools → END
```
- **resolve_tenant** — maps assistant-id / dialled-number / widget-key / explicit id → tenant, loads cached config.
- **emergency_check** — deterministic per-trade keyword classifier (no LLM hop on the safety path).
- **reason** — streams LLM tokens (`astream`, never `ainvoke`) with the 5 native tools bound.
- **tools** — executes native tools, loops back.

**The single seam:** every channel calls `stream_turn()` in `app/brain/runner.py`, which
yields `BrainEvent`s (`token`/`acknowledgement`/`tool_start`/`tool_result`/`final`/`error`).
Adapters just re-encode that stream. **No business logic in adapters. No graph node names a
vendor.**

**Two tool tiers** (CLAUDE.md convention #2): native critical-path tools
(`check_availability`, `book_job`, `send_confirmation`, `escalate`, `is_emergency`) vs
future MCP long-tail. Tools read `tenant_id` from `RunnableConfig`, never from a model
argument — that's what stops the LLM crossing tenants.

**Responses are AI-generated**, not hardcoded. Every reply comes from the LLM shaped by the
system prompt. The only fixed strings: the greeting (spoken instantly, zero latency),
acknowledgement fillers, and a fallback apology.

---

## 5. Repo map (key files)

```
app/
  brain/
    graph.py            the LangGraph graph (START→resolve→emergency→reason⇄tools)
    runner.py           stream_turn() — THE seam every channel uses; BrainEvent
    llm.py              provider factory: groq / openai / google  ← add providers here
    metrics.py          per-turn LLM request counting
    sanitize.py         tool-call-leak recovery + repeat suppression (weak-model armor)
    nodes/reason.py     LLM call, history trimming, retry logic, tool-unsupported detection
    prompts/system.py   fills content/system-prompt.md with per-tenant values
  channels/
    chat.py             POST /chat  (web chat, SSE)
    vapi_llm.py         POST /chat/completions  (Vapi Custom-LLM shim)
    webhooks.py         POST /webhooks/vapi  (end-of-call → calls record)
    vapi_provisioning.py + vapi_schema.py + openai_compat.py + security.py
  tools/                booking/ + messaging/ providers (stub live, google/twilio stubbed)
  tenancy/              models, repository, cached loader (data path = content/tenants/)
  db/                   models, in-memory store (Supabase = Phase 4 stub)
  config.py             Settings (env), active_model, content_dir, tenant_data_dir
  main.py               FastAPI app; GET /health shows provider+model+tenants
content/                ← ALL USER-EDITABLE CONTENT (see content/README.md)
  system-prompt.md      the bot's instructions (${placeholder} template, hot-reloads)
  acknowledgements.json filler phrases (hot-reloads)
  tenants/*.json        per-business config (name, services, hours, greeting, voice, vapi)
scripts/
  chat_cli.py           terminal chat (banner shows model=provider/name); /jobs /reset /quit
  provision_vapi.py     create/update Vapi assistant; --attach-number / --detach-number / --show / --dry-run
  check_model.py        preflight: model reachable + supports tool calling; --all surveys
  onboard_tenant.py     Phase 4 stub
infra/Dockerfile        one-service image (COPYs content/)
tests/                  187 tests; conftest has ScriptedChatModel + hermetic_settings
.env                    LLM_PROVIDER, keys, Vapi/Cartesia/Supabase — gitignored, ROOT ONLY
```

---

## 6. To change the bot (no code)

`content/` folder, hot-reloads on next message:
- **Behaviour / tone / rules** → `content/system-prompt.md` (keep `${...}` slots).
- **Business details** → `content/tenants/hotel-mzv.json`.
- **Filler lines** → `content/acknowledgements.json`.
- **Model + keys** → `.env` at root (NOT in content/, for secret safety).

After editing a tenant's `voice`/`vapi` block, push to Vapi:
`python -m scripts.provision_vapi --tenant hotel-mzv`.

---

## 7. Gotchas / hard-won lessons (DO NOT rediscover these)

1. **`uuid_utils` must be 0.12–0.15 on this Windows box.** Builds 0.16+ are blocked by
   Windows Application Control (`DLL load failed ... Application Control policy has blocked
   this file`). langchain-core 1.x hard-imports it, so one blocked build breaks *every*
   import. The `google` extra caps it (`<0.16` on win32). Linux deploy unaffected.
2. **Gemini 3.x needs "thought signatures"** on multi-turn tool calls — only
   `langchain-google-genai` **4.x** supports them (older 400s on the 2nd tool turn). Gemini
   2.x works on any version but has low free limits. This forced the modern langchain stack.
3. **If ToolNode import fails** after a pip churn: `pip install --force-reinstall --no-deps
   langgraph langgraph-prebuilt` (they half-install over each other's `langgraph/prebuilt/`).
4. **Vapi has TWO auth destinations, one secret.** `server.secret` covers webhooks
   (`server.url`); the custom-LLM endpoint (`model.url`) needs `model.headers` with
   `x-vapi-secret`. Miss it → caller hears the greeting then silence (every turn 401s).
   `build_assistant_payload` sets both from `VAPI_WEBHOOK_SECRET`.
5. **Groq free tier = 12k tokens/min, ~1,460 fixed tokens/request** (system prompt + tool
   schemas). One booking turn = 3 requests. Voice conversations blow the free cap fast — the
   reason we moved to Gemini's higher limits. Latency to Google may be worse than Groq for
   voice, though — measure before trusting for phone.
6. **Groq `groq/compound` and several models don't support tool calling** — they chat but
   never book (silent failure). Always run `check_model` after changing model.
7. **Retry policy:** malformed-tool-call → retry once tools-withheld; quota/auth (429/401)
   → do NOT retry (wastes a billable request). SDK `max_retries=0` so the SDK's 3–18s
   internal backoff doesn't get the call killed by Vapi.
8. **Bookings are in RAM** — they vanish on server restart. Real persistence is Phase 4.

---

## 8. How to run

```powershell
# terminal chat (banner shows the active model)
.venv\Scripts\python.exe -m scripts.chat_cli --tenant hotel-mzv
#   commands inside: /jobs  /reset  /quit

# confirm which model + tool-calling support
.venv\Scripts\python.exe -m scripts.check_model            # current
.venv\Scripts\python.exe -m scripts.check_model --all      # survey the key's models

# voice: server + tunnel + provision, then web-call from Vapi dashboard
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
ngrok http 8000 --domain=ranked-wielder-clarify.ngrok-free.dev
.venv\Scripts\python.exe -m scripts.provision_vapi --tenant hotel-mzv
#   real phone: add --attach-number +1XXXXXXXXXX  (needs Twilio creds or Vapi provisions one)

# tests + lint
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
```

`.venv` note: bare `python`/`pip` on this box point at MISMATCHED interpreters (3.14 pip,
3.12 python). Always use `.venv\Scripts\python.exe` or activate the venv first.

---

## 9. What's next / open decisions

**Immediate, optional:** convert `hotel-mzv` from electrician → real hotel (edit
`content/tenants/hotel-mzv.json` services + emergency keywords, tweak `system-prompt.md`).

**To advance phases (recommended order 3 → 4 → 5 → 6 → 7):**
- **Phase 3** is the biggest value and is **blocked on ONE decision**: booking provider.
  Plan recommends **Google Calendar** (default), Cal.com or Supabase-native as alternatives.
  Then wire Twilio SMS + Vapi warm transfer (interfaces already exist: `app/tools/booking/`,
  `app/tools/messaging/`, `Escalator.can_transfer`).
- **Phase 4** — Supabase + RLS behind the existing store/repository protocols; voice cloning
  (needs stored written consent — non-negotiable); encrypted secrets.
- **Phase 5** — build the embeddable JS widget against the working `/chat` endpoint.
- **Phase 7** — deploy the one service to a US region (co-locate with LLM/Vapi to fix
  latency), turn on LangSmith, start A2P 10DLC for SMS.

**Token-reduction ideas (raised, not yet done):** unbind `is_emergency` from the model
(~100 tok/req; classifier node already runs anyway); fold `send_confirmation` into `book_job`
(removes a request per booking). ~30% off a booking conversation.

**Pending §16 decisions still open:** booking provider (blocks Phase 3), whose voice to
clone (+consent), avatar now/later, web-only vs WhatsApp.
