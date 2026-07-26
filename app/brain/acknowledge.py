"""Acknowledge-then-act fillers.

CLAUDE.md convention #1: the first spoken response must never wait on a tool
call. The system prompt asks the model to acknowledge before acting; this is
the safety net for when it doesn't — the runner speaks one of these the moment
a slow tool starts with nothing said yet, so the caller never hears dead air
(plan §13).

The phrases live in `content/acknowledgements.json` so they can be edited
without touching code.
"""

from __future__ import annotations

import json
import random
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

_DEFAULT_KEY = "default"
_rng = random.Random()


@lru_cache(maxsize=4)
def _load(path_str: str, _mtime: float) -> dict[str, tuple[str, ...]]:
    """Load and cache the phrase lists. `_mtime` keys the cache so edits to the
    file take effect on the next turn without a restart."""
    raw = json.loads(Path(path_str).read_text(encoding="utf-8"))
    return {
        key: tuple(value)
        for key, value in raw.items()
        if not key.startswith("_") and isinstance(value, list)
    }


def _fillers() -> dict[str, tuple[str, ...]]:
    path = get_settings().content_dir / "acknowledgements.json"
    return _load(str(path), path.stat().st_mtime)


def acknowledgement_for(tool_name: str, channel: str = "chat") -> str:
    """A short, natural line to say while `tool_name` runs.

    Looks for a `"{tool_name}.{channel}"` key first (e.g. `escalate.voice`),
    falling back to the bare tool name and then the shared default. This
    matters for `escalate`: "putting you through now" is only true on voice —
    saying it on chat, where nothing is transferred, would be a lie.
    """
    fillers = _fillers()
    choices = (
        fillers.get(f"{tool_name}.{channel}")
        or fillers.get(tool_name)
        or fillers.get(_DEFAULT_KEY)
        or ("One moment.",)
    )
    return _rng.choice(choices)


def seed(value: int) -> None:
    """Make filler choice deterministic (tests, demos)."""
    _rng.seed(value)
