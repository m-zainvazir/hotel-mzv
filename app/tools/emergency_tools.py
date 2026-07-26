"""Native emergency-check tool — tier 1 (critical path).

The `emergency_check` graph node already runs this classifier on every inbound
turn; the tool exists so the model can also check a paraphrase it is unsure
about ("she said the outlet went bang") without an extra LLM hop.
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.tools.context import tenant_from_config
from app.tools.emergency import classify_emergency


@tool
async def is_emergency(utterance: str, config: RunnableConfig = None) -> str:
    """Check whether something the caller said is a safety emergency for this trade.

    `utterance` is the caller's words. Returns EMERGENCY or NOT_EMERGENCY plus
    the safety line to say if it is one.
    """
    tenant = tenant_from_config(config)
    verdict = classify_emergency(utterance, tenant)

    if not verdict.is_emergency:
        return "NOT_EMERGENCY — continue the normal booking flow."
    return (
        f"EMERGENCY ({verdict.reason}). Say this first: "
        f"{tenant.emergency.holding_message!r} Then call escalate."
    )
