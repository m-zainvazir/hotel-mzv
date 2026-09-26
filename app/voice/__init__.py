"""The voice relay (Phase 9.3) — browser mic in, our own STT/LLM/TTS out.

Deliberately a top-level package rather than more files under
`app/channels/`: `channels/` holds adapters, and everything here except
`app/channels/voice_live.py` (the WebSocket route) is provider-facing
infrastructure that adapter merely drives. The same split `app/tools/` and
`app/tools/booking/` already have.

Nothing in here knows the brain exists beyond `stream_turn` — and nothing in
the brain knows this package exists at all (CLAUDE.md convention #4).
"""
