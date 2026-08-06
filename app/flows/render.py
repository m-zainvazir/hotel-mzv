"""Rendering a flow node — the part with no model in it (Phase 9.2).

A node's text is a fixed string, so it is emitted as one `token` event
rather than chunked: there is nothing to stream, and faking a token-by-token
trickle would only add latency to the one path whose whole selling point is
that it has none.

The non-obvious half is `_remember`. A flow turn bypasses the graph
entirely, which means LangGraph's checkpointer never sees it — and the
*next* free-text turn would then have no idea the visitor just clicked
through three buttons ("what were those options again?" would be answered
from an empty transcript). So the exchange is written back explicitly. It is
the single most important line in this package, and the easiest one to omit
without noticing: nothing fails, the model just quietly has amnesia.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage

from app.brain.events import BrainEvent
from app.flows.resolver import ResolvedFlow
from app.tenancy.models import TenantConfig

logger = logging.getLogger(__name__)


async def stream_flow(
    tenant: TenantConfig,
    resolved: ResolvedFlow,
    *,
    session_id: str,
    asked: str,
) -> AsyncIterator[BrainEvent]:
    """Emit one scripted node, then record it in the conversation.

    `asked` is what the visitor's click should read as in the transcript —
    the button's own label. It becomes the `HumanMessage` half of the
    write-back, so the model later sees a coherent "user said X, I said Y"
    exchange rather than an assistant message that arrived from nowhere.
    """
    node = resolved.node

    yield BrainEvent("token", node.say)
    if resolved.buttons:
        yield BrainEvent("actions", data={"actions": resolved.buttons})

    await _remember(tenant, session_id, asked=asked, said=node.say)

    yield BrainEvent("final", node.say, data={"llm_requests": 0, "flow_id": node.id})


async def _remember(tenant: TenantConfig, session_id: str, *, asked: str, said: str) -> None:
    """Write the flow exchange into the checkpointer thread.

    Never raises. A checkpointer hiccup should degrade the *next* turn's
    context, never break the button click that is already half-rendered on
    the visitor's screen — the same posture `_record_chat_message`
    (app/channels/chat.py) takes for transcript writes.

    Imported inside the function because `app.brain.graph` pulls in the
    whole node/tool graph, and this module is imported from `app.flows`'s
    package init.
    """
    try:
        from app.brain.graph import get_graph
        from app.brain.runner import thread_config

        await get_graph().aupdate_state(
            thread_config(tenant.tenant_id, session_id, channel="chat"),
            {"messages": [HumanMessage(content=asked), AIMessage(content=said)]},
        )
    except Exception:
        logger.exception(
            "could not record flow turn in the checkpointer tenant=%s session=%s",
            tenant.tenant_id,
            session_id,
        )
