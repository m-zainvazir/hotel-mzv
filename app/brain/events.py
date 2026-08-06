"""The event vocabulary every channel speaks.

Split out of `app/brain/runner.py` in Phase 9.2 for one reason: `app/flows/`
produces `BrainEvent`s and the runner consumes `app/flows/`, so leaving the
dataclass in `runner` would have made that a circular import. Nothing else
changed — `runner` re-exports both names, so every existing
`from app.brain.runner import BrainEvent` still works.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EventType = Literal[
    "token",
    "acknowledgement",
    "tool_start",
    "tool_result",
    "suggestions",
    "handoff",
    "actions",
    "cards",
    "final",
    "error",
]


@dataclass(slots=True)
class BrainEvent:
    type: EventType
    text: str = ""
    tool: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_spoken(self) -> bool:
        """True for events that become audio / visible text."""
        return self.type in ("token", "acknowledgement")
