# Start here — continuing in Claude Code

This folder is your repo root. Reference files:
- `AI-Receptionist-Build-Plan.md` — the canonical A-Z spec (architecture, phases, costs, decisions).
- `CLAUDE.md` — quick-reference + conventions Claude Code reads every session.

## How to start
1. Open a terminal in this folder: `D:\Projects\My\AI-Reception`
2. Run `claude`
3. Paste the kickoff prompt below.

## Before you start (Phase 0)
- Make the five decisions in §16 of the plan.
- Have your API keys ready (Vapi, Groq, Supabase, Twilio, Cartesia, hosting, Google/Cal.com). Claude Code will generate a `.env.example` — you fill in the real values in `.env`.

## Kickoff prompt (copy-paste this)

> Read `AI-Receptionist-Build-Plan.md` in full and `CLAUDE.md`. Then:
> 1. Scaffold the repository structure from §18 of the plan (Python + FastAPI + LangGraph).
> 2. Generate a `.env.example` listing every API key/secret the plan requires, grouped by service.
> 3. Implement **Phase 1** (§15): a single-tenant LangGraph brain using Groq (Llama 3.3 70B) with the native tools stubbed (booking returns fake slots).
> 4. Acceptance criterion: I can run it locally and chat with it in the terminal, and it "books" a fake job end to end.
>
> Follow the conventions in `CLAUDE.md` — especially token streaming and the two-tool-tier rule. Write tests. When done, summarize the structure you created and how to run it.

## Follow-on prompt (reuse for each later phase)

> Implement Phase N (§15 of `AI-Receptionist-Build-Plan.md`). Meet its acceptance criterion, follow `CLAUDE.md` conventions, and write tests before marking it done.

Recommended order: Phase 2 (Vapi voice) → 3 (real tools) → 4 (multi-tenancy) → 5 (chatbot) → 6 (MCP) → 7 (deploy) → 8 (avatar).

## Tip
Run `/init` once in Claude Code and it will expand `CLAUDE.md` with things it learns about the codebase as it grows. Keep the plan doc and `CLAUDE.md` in version control so the spec travels with the code.
