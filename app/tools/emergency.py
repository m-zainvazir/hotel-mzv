"""Deterministic emergency classifier.

Runs on the critical path *before* the model sees the turn, so an unsafe
utterance is flagged in microseconds rather than costing an LLM round-trip
(plan §9, feature 5). Patterns are per-tenant config, so a plumber tenant
matches "gas leak" and an electrician tenant matches "arcing".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from app.tenancy.models import TenantConfig


@dataclass(frozen=True)
class EmergencyVerdict:
    is_emergency: bool
    matched: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reason(self) -> str:
        if not self.is_emergency:
            return ""
        return "caller mentioned: " + ", ".join(self.matched)


@lru_cache(maxsize=64)
def _compile(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE))
        except re.error:  # a tenant typo must never take the brain down
            compiled.append(re.compile(re.escape(pattern), re.IGNORECASE))
    return tuple(compiled)


def classify_emergency(text: str, tenant: TenantConfig) -> EmergencyVerdict:
    """Flag trade-specific danger patterns in a single caller utterance."""
    if not text or not text.strip():
        return EmergencyVerdict(False)

    matches: list[str] = []
    for pattern in _compile(tuple(tenant.emergency.keywords)):
        found = pattern.search(text)
        if found:
            matches.append(found.group(0).lower())

    unique = tuple(dict.fromkeys(matches))
    return EmergencyVerdict(bool(unique), unique)
