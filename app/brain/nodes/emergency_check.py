"""Node 2 — fast, deterministic safety classifier (plan §5, step 2).

Deliberately not an LLM call: it must add microseconds, not a round-trip, and
it must not be talked out of firing.
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage

from app.brain.state import ReceptionistState
from app.tenancy.loader import get_tenant_config
from app.tools.emergency import classify_emergency

logger = logging.getLogger(__name__)


async def emergency_check(state: ReceptionistState) -> dict:
    messages = state.get("messages") or []
    latest = next(
        (m for m in reversed(messages) if isinstance(m, HumanMessage)),
        None,
    )
    if latest is None:
        return {}

    tenant = get_tenant_config(state["tenant_id"])
    verdict = classify_emergency(_text_of(latest), tenant)

    if not verdict.is_emergency:
        # Never clear a flag raised earlier in the same conversation.
        return {} if state.get("emergency") else {"emergency": False}

    logger.warning("emergency detected tenant=%s reason=%s", tenant.tenant_id, verdict.reason)
    return {"emergency": True, "emergency_reason": verdict.reason, "intent": "emergency"}


def _text_of(message: HumanMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    # Multimodal content blocks — pull out the text parts.
    return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
