"""start_flow — the model's way *into* a deterministic flow (Phase 9.2).

A flow is normally entered by clicking a button, which never touches a model
at all (`app/brain/runner.py`'s postback short-circuit). But a visitor who
types "I need to find a clinic near me" instead of clicking `📍 Find a
Location` should land in exactly the same place — that's the "TRIGGER: user
chooses X **or expresses intent to** Y" pattern every scripted bot needs and
only an LLM can actually do. This tool is that bridge, and the only one.

The important half is what happens *after* it returns: its artifact
(`kind: "flow"`) routes the graph to END rather than back to `reason`
(`app/brain/graph.py::_after_tools`). Termination is a graph edge, not a
prompt instruction — "say nothing after this" is a request a model honours
most of the time, and a scripted flow that sometimes gets a chatty sentence
bolted onto it is not deterministic in any useful sense.

Chat-only and conditionally bound (`native_tools_for`), like `offer_actions`
and `search_knowledge`.
"""

from __future__ import annotations

import logging

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.flows.resolver import resolve_flow
from app.tools.context import tenant_from_config

logger = logging.getLogger(__name__)


@tool(response_format="content_and_artifact")
async def start_flow(flow_id: str, config: RunnableConfig = None) -> tuple[str, dict]:
    """Hand the conversation to one of the scripted flows listed under
    "Flows you can start" in your instructions.

    Call this as soon as what the caller wants matches one of them — don't
    paraphrase the flow's message yourself first. The flow's own wording and
    buttons are shown to the caller automatically, and your turn ends there,
    so say nothing before or after calling it.
    """
    tenant = tenant_from_config(config)
    resolved = resolve_flow(tenant, flow_id)

    if resolved is None:
        # Recoverable on purpose: the model gets the real list back and can
        # pick again on the next hop, exactly like find_service's miss.
        logger.warning("start_flow: unknown flow %r for tenant %s", flow_id, tenant.tenant_id)
        known = ", ".join(flow.id for flow in tenant.flows) or "(none configured)"
        return f"No flow called {flow_id!r}. Available flows: {known}.", {}

    return resolved.node.say, {
        "kind": "flow",
        "flow_id": resolved.node.id,
        "say": resolved.node.say,
        "actions": resolved.buttons,
    }
