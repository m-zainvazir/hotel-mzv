"""Deterministic flows and generic-template cards (Phase 9.2).

This package is the answer to a specific failure mode: a chat script that
tries to make an LLM behave like a state machine. Written as a prompt, "show
exactly this text and these three buttons, then STOP" is a request the model
honours most of the time — and a `Main Menu` button should not be a
probability. Here it is a config lookup with no model in the loop at all.

Three modules, one rule each:

* `resolver` — the single slug -> button-row resolver. Everything that
  renders a button (the greeting menu, a flow node, `offer_actions`, a
  card's buttons) goes through it, so there is exactly one place that
  decides what a `TenantLink` looks like on the wire.
* `render` — turns a resolved node into `BrainEvent`s, and writes the
  exchange back to the checkpointer so the *next* free-text turn knows what
  the visitor just navigated through.
* `cards` — sanitises model-supplied card URLs before any of them reach a
  browser.

It lives under `app/` beside the brain rather than inside a channel because
of CLAUDE.md's "never put business logic in a channel adapter" — this is the
feature most tempted to break that rule, and keeping it here is what lets a
flow turn inherit rate limiting, transcript persistence, the channel-enabled
check and the draft-preview override without re-implementing any of them.
"""

from __future__ import annotations

from app.flows.cards import sanitize_cards
from app.flows.render import stream_flow
from app.flows.resolver import ResolvedFlow, resolve_buttons, resolve_flow, resolve_menu

__all__ = [
    "ResolvedFlow",
    "resolve_buttons",
    "resolve_flow",
    "resolve_menu",
    "sanitize_cards",
    "stream_flow",
]
