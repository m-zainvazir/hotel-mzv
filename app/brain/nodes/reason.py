"""Node 3 — reason with the LLM and stream tokens as they're produced.

The model is invoked with `astream` (never `ainvoke`) so LangGraph emits token
chunks the instant the provider produces them. Buffering the full reply here
would silently break the latency budget everywhere downstream.
"""

from __future__ import annotations

import logging
from datetime import datetime

from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage, trim_messages
from langchain_core.runnables import RunnableConfig

from app.brain.llm import get_llm
from app.brain.metrics import record_llm_request
from app.brain.prompts.system import render_system_prompt
from app.brain.sanitize import extract_inline_tool_calls
from app.brain.state import ReceptionistState
from app.config import get_settings
from app.mcp.client import load_mcp_tools
from app.tenancy.loader import tenant_config_from_runnable
from app.tools.booking.schedule import business_hours_for
from app.tools.registry import native_tools_for

logger = logging.getLogger(__name__)

_REPAIR_NOTE = (
    "Your last tool call was rejected by the provider as malformed. Do not call a "
    "tool this turn. Reply to the caller in plain words, and if you still need "
    "something from a tool, ask them a short question that lets you retry next turn."
)

#: Last resort when the model is unreachable. Silence is the worst outcome on a
#: live call, so we always say something.
_FALLBACK_LINE = "Sorry — I didn't quite catch that. Could you say it again?"


async def reason(state: ReceptionistState, config: RunnableConfig) -> dict:
    tenant = tenant_config_from_runnable(state["tenant_id"], config)
    channel = state.get("channel", "chat")

    # Tier 1 first, tier 2 appended — MCP is the long tail, never the fast path.
    tools = native_tools_for(tenant, channel) + await load_mcp_tools(tenant)

    model = get_llm()
    if tools:
        model = model.bind_tools(tools)

    # Phase 9.4: opening hours come from whoever actually owns them — the
    # tenant's calendar, not its config. Cached per tenant, so this is an
    # in-memory read on all but the first turn after a config change; it
    # never raises (see that module's docstring).
    business_hours = await business_hours_for(tenant)

    prompt = [
        SystemMessage(
            render_system_prompt(
                tenant,
                channel=channel,
                now=datetime.now(tenant.tz),
                emergency=bool(state.get("emergency")),
                emergency_reason=state.get("emergency_reason"),
                business_hours=business_hours,
            )
        ),
        *_recent(state.get("messages", [])),
    ]

    try:
        aggregated = await _stream_reply(model, prompt, config)
    except Exception as exc:
        if _tool_calling_unsupported(exc):
            # Retrying without tools "works" — it talks, and books nothing, for
            # every call from now on. That is a configuration error wearing a
            # working assistant's clothes, so say so as loudly as possible.
            logger.critical(
                "MODEL CANNOT CALL TOOLS: %r. Booking, availability and escalation "
                "will never work with it. Set a tool-capable model "
                "(`python -m scripts.check_model --all` lists yours). Provider said: %s",
                get_settings().groq_model,
                exc,
            )
        if not _worth_retrying(exc):
            # Quota and auth failures will fail again identically. Retrying
            # spends a second billable request to learn nothing.
            logger.error("model unavailable (%s) — not retrying", exc)
            aggregated = AIMessage(content=_FALLBACK_LINE)
        else:
            # Observed on Groq: "Failed to call a function" when the model emits
            # a malformed call (plan §17). A caller must never get silence for
            # it, so retry once with tools withheld — that can only make speech.
            logger.warning("model call failed (%s) — retrying with tools withheld", exc)
            try:
                aggregated = await _stream_reply(
                    get_llm(), [*prompt, SystemMessage(_REPAIR_NOTE)], config
                )
            except Exception:
                logger.exception("retry failed — falling back to a spoken apology")
                aggregated = AIMessage(content=_FALLBACK_LINE)

    if aggregated is None:  # provider returned nothing at all
        aggregated = AIMessage(content="")

    return {"messages": [_recover_inline_tool_calls(aggregated)]}


def _recent(messages: list) -> list:
    """The tail of the conversation, bounded.

    Every request re-sends the whole history, so an unbounded transcript is a
    compounding cost: a long call eventually spends more tokens re-reading
    itself than on the caller. Trimming is delegated to `trim_messages` rather
    than sliced by hand because a naive cut can orphan a ToolMessage from the
    AIMessage that requested it, which providers reject outright.
    """
    limit = get_settings().llm_history_messages
    if len(messages) <= limit:
        return list(messages)

    return trim_messages(
        messages,
        strategy="last",
        token_counter=len,  # count messages, not tokens — cheap and predictable
        max_tokens=limit,
        start_on="human",  # keeps tool-call/result pairs intact
        include_system=False,  # the system prompt is rendered fresh each turn
        allow_partial=False,
    )


#: Failures that a second identical request cannot fix.
_UNRETRYABLE = (
    "rate_limit",
    "rate limit",
    "429",
    "quota",
    "insufficient_quota",
    "invalid_api_key",
    "authentication",
    "401",
    "403",
)


def _worth_retrying(exc: Exception) -> bool:
    """Only retry failures a tools-withheld attempt could actually resolve."""
    text = str(exc).lower()
    return not any(marker in text for marker in _UNRETRYABLE)


def _tool_calling_unsupported(exc: Exception) -> bool:
    """The model is reachable but refuses tools — e.g. Groq's `groq/compound`."""
    text = str(exc).lower()
    return "tool calling" in text and "not supported" in text


async def _stream_reply(model, prompt, config) -> AIMessage | AIMessageChunk | None:
    # Every call here is one billable provider request. A single conversational
    # turn costs one, plus one more per tool hop, plus one per retry — which is
    # why request counts climb far faster than turn counts.
    count = record_llm_request()
    logger.debug("llm request #%d (%d messages in prompt)", count, len(prompt))

    aggregated: AIMessage | AIMessageChunk | None = None
    async for chunk in model.astream(prompt, config=config):
        aggregated = chunk if aggregated is None else _merge(aggregated, chunk)
    return aggregated


def _recover_inline_tool_calls(message: AIMessage) -> AIMessage:
    """Promote text-encoded tool calls to real ones (see app.brain.sanitize)."""
    if not isinstance(message.content, str):
        return message

    cleaned, recovered = extract_inline_tool_calls(message.content)
    if not recovered:
        return message

    # A structured call always wins; only fill the gap when there is none.
    calls = list(message.tool_calls or []) or recovered
    return AIMessage(
        content=cleaned,
        tool_calls=calls,
        id=message.id,
        response_metadata=message.response_metadata,
        usage_metadata=message.usage_metadata,
    )


def _merge(left, right):
    """Concatenate streamed chunks, tolerating models that yield whole messages."""
    if isinstance(left, AIMessageChunk) and isinstance(right, AIMessageChunk):
        return left + right
    return right if isinstance(left, AIMessage) and not left.content else left
