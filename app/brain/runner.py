"""Streaming runner — the one place channels talk to the brain.

Every adapter (terminal CLI, `/chat` SSE, Vapi `/chat/completions`) consumes
this async iterator and re-encodes it for its own transport. That's what keeps
business logic out of the adapters.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable
from typing import Any, Literal
from urllib.parse import urlparse

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from app.brain.acknowledge import acknowledgement_for
from app.brain.events import BrainEvent, EventType
from app.brain.graph import get_graph
from app.brain.metrics import TurnCounter
from app.brain.sanitize import InlineToolCallFilter, RepeatSuppressor, extract_urls
from app.flows.render import stream_flow
from app.flows.resolver import parse_postback, resolve_flow
from app.tenancy.admin import get_draft
from app.tenancy.loader import resolve_tenant_id, tenant_config_from_runnable
from app.tenancy.models import TenantConfig
from app.tools.registry import is_slow_tool

logger = logging.getLogger(__name__)

# Re-exported: these moved to app/brain/events.py in Phase 9.2 (app/flows/
# produces them and this module consumes app/flows/, so leaving them here
# would be a cycle). Every existing `from app.brain.runner import BrainEvent`
# keeps working.
__all__ = ["BrainEvent", "EventType", "run_turn", "stream_turn", "thread_config", "thread_is_cold"]

MAX_TOOL_HOPS = 12


async def stream_turn(
    *,
    text: str,
    tenant_id: str | None = None,
    session_id: str = "local",
    channel: str = "chat",
    called_number: str | None = None,
    widget_key: str | None = None,
    assistant_id: str | None = None,
    history: list[AnyMessage] | None = None,
    tenant_config_variant: Literal["live", "draft"] = "live",
    postback: str | None = None,
) -> AsyncIterator[BrainEvent]:
    """Run one conversational turn, yielding events as they happen.

    `history` seeds a *cold* thread and nothing else. Voice orchestrators resend
    the whole transcript every turn while our checkpointer already holds it, so
    passing history on a warm thread would double every message. The caller is
    responsible for checking `thread_is_cold()` first.

    `tenant_config_variant="draft"` is the Test Agent's "Preview draft" link
    only — reached exclusively via a verified widget-token `variant` claim
    (`app/channels/widget_auth.py`), never from a request body. Resolves the
    tenant's *current* draft fresh, every turn (never cached, never baked
    into a token, so further edits show up mid-conversation and a deploy or
    discard is reflected on the very next message) and threads it through
    `RunnableConfig.configurable["tenant_config_override"]` for this thread
    only — see `app/tenancy/loader.py::tenant_config_from_runnable` for why
    that's the only place a draft is allowed to reach a live conversation.
    Falls back to live silently if there's no draft (the same "effective
    config" the Config tab editor itself shows).

    `postback` (Phase 9.2) is a `flow:<id>` string from a clicked button. It
    short-circuits into `app/flows/` and never reaches the graph, so a
    scripted flow node costs zero LLM requests — that determinism is the
    whole feature. Chat only, and it degrades to an ordinary model turn for
    anything it can't resolve, so a stale button in a tab left open across a
    deploy still gets a sensible answer.
    """
    resolved = resolve_tenant_id(
        tenant_id=tenant_id,
        assistant_id=assistant_id,
        phone_number=called_number,
        widget_key=widget_key,
    )

    tenant_config_override = None
    if tenant_config_variant == "draft":
        tenant_config_override, _ = await get_draft(resolved)

    # After the draft override is resolved, so a "Preview draft" session
    # navigates the *draft's* flows; before the graph, so a flow turn never
    # builds a model request at all.
    flow_id = parse_postback(postback) if channel == "chat" else None
    if flow_id:
        tenant = tenant_config_from_runnable(
            resolved, {"configurable": {"tenant_config_override": tenant_config_override}}
        )
        flow = resolve_flow(tenant, flow_id)
        if flow is not None:
            async for event in stream_flow(tenant, flow, session_id=session_id, asked=text):
                yield event
            return
        logger.info(
            "postback %r did not resolve for tenant %s — falling through to the model",
            postback,
            resolved,
        )

    config = thread_config(
        resolved,
        session_id,
        channel=channel,
        called_number=called_number,
        widget_key=widget_key,
        tenant_config_override=tenant_config_override,
    )
    inputs = {
        "messages": [*(history or []), HumanMessage(content=text)],
        "tenant_id": resolved,
        "channel": channel,
    }

    counter = TurnCounter()
    spoken: list[str] = []
    #: What was said in the current reply segment (reset at each tool hop).
    segment: list[str] = []
    spoke_since_last_tool = False
    #: URLs already offered as real `offer_actions` buttons this turn — the
    #: end-of-turn auto-linkify pass below skips these so a catalog link the
    #: model correctly called a tool for never doubles up.
    offered_urls: set[str] = set()
    # Guards against a model that writes its tool call into the reply text.
    safe_text = InlineToolCallFilter()
    # Guards against a model that repeats itself after a tool returns.
    no_repeat = RepeatSuppressor()

    try:
        async for mode, payload in get_graph().astream(
            inputs, config=config, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                chunk, meta = payload
                if meta.get("langgraph_node") != "reason":
                    continue
                content = no_repeat.feed(safe_text.feed(_as_text(getattr(chunk, "content", ""))))
                if content:
                    spoken.append(content)
                    segment.append(content)
                    spoke_since_last_tool = True
                    yield BrainEvent("token", content)

            elif mode == "updates":
                for node, update in (payload or {}).items():
                    if node == "reason":
                        tail = no_repeat.feed(safe_text.flush()) + no_repeat.flush()
                        if tail:
                            spoken.append(tail)
                            segment.append(tail)
                            spoke_since_last_tool = True
                            yield BrainEvent("token", tail)

                        for call in _tool_calls(update):
                            name = call.get("name", "")
                            if not spoke_since_last_tool and is_slow_tool(name):
                                # Trailing space, or it collides with whatever
                                # the model says next: "the diary.I'll text you".
                                ack = acknowledgement_for(name, channel).rstrip() + " "
                                spoken.append(ack)
                                segment.append(ack)
                                spoke_since_last_tool = True
                                yield BrainEvent("acknowledgement", ack, tool=name)
                            yield BrainEvent(
                                "tool_start", tool=name, data=dict(call.get("args") or {})
                            )
                    elif node == "tools":
                        spoke_since_last_tool = False
                        # Llama often re-speaks its pre-tool acknowledgement in
                        # the follow-up segment. Harmless in text, jarring aloud.
                        no_repeat.arm("".join(segment))
                        segment = []
                        for message in _messages(update):
                            yield BrainEvent(
                                "tool_result",
                                text=_as_text(message.content),
                                tool=getattr(message, "name", None),
                            )
                            suggestions = _suggestions_artifact(message)
                            if suggestions is not None:
                                yield BrainEvent(
                                    "suggestions",
                                    tool=getattr(message, "name", None),
                                    data=suggestions,
                                )
                            handoff = _handoff_artifact(message)
                            if handoff is not None:
                                yield BrainEvent(
                                    "handoff", tool=getattr(message, "name", None), data=handoff
                                )
                            actions = _actions_artifact(message)
                            if actions is not None:
                                offered_urls.update(
                                    a["url"]
                                    for a in actions.get("actions", [])
                                    if a.get("type") == "link" and a.get("url")
                                )
                                yield BrainEvent(
                                    "actions", tool=getattr(message, "name", None), data=actions
                                )
                            cards = _cards_artifact(message)
                            if cards is not None:
                                offered_urls.update(
                                    url
                                    for card in cards.get("cards", [])
                                    for url in (
                                        card.get("url"),
                                        *(b.get("url") for b in card.get("buttons", [])),
                                    )
                                    if url
                                )
                                yield BrainEvent(
                                    "cards", tool=getattr(message, "name", None), data=cards
                                )
                            flow = _flow_artifact(message)
                            if flow is not None:
                                # The node's own text, spoken as if the model
                                # had written it — `_after_tools` has already
                                # guaranteed it gets no chance to add more.
                                say = flow.get("say", "")
                                if say:
                                    spoken.append(say)
                                    segment.append(say)
                                    yield BrainEvent("token", say)
                                if flow.get("actions"):
                                    yield BrainEvent(
                                        "actions",
                                        tool=getattr(message, "name", None),
                                        data={"actions": flow["actions"]},
                                    )
    except Exception as exc:  # a channel must always get a terminal event
        logger.exception("brain turn failed tenant=%s session=%s", resolved, session_id)
        yield BrainEvent("error", str(exc))
        return

    if counter.requests > 2:
        logger.info("turn used %d llm requests (1 + tool hops + retries)", counter.requests)

    full_text = "".join(spoken)
    if channel == "chat":
        # Fallback for a URL the model wrote straight into its reply — e.g.
        # an instruction typed into the AI Prompt rather than registered as
        # a `links` catalog entry — so it's at least a real clickable link
        # instead of dead text. The catalog + offer_actions path (above)
        # stays the "right" way: there the URL never enters the reply text
        # at all, so nothing is ever duplicated and a hallucinated URL from
        # a tool result can't become clickable. This can duplicate the URL
        # (it's already been streamed as text by the time it's detected —
        # tokens can't be un-sent) — a visible trade-off, not a bug.
        auto_urls = [u for u in extract_urls(full_text) if u not in offered_urls]
        if auto_urls:
            yield BrainEvent("actions", data={"actions": _auto_link_actions(auto_urls)})

    yield BrainEvent("final", full_text, data={"llm_requests": counter.requests})


def thread_config(
    tenant_id: str,
    session_id: str,
    *,
    channel: str = "chat",
    called_number: str | None = None,
    widget_key: str | None = None,
    tenant_config_override: TenantConfig | None = None,
) -> dict[str, Any]:
    """The RunnableConfig for one conversation.

    Thread ids are tenant-prefixed so two tenants can never share conversation
    state, even if a session or call id collides.

    `tenant_config_override`, when set, is what
    `app/tenancy/loader.py::tenant_config_from_runnable` hands back instead
    of the shared cache — see `stream_turn`'s docstring. It travels only as
    far as this one `RunnableConfig` dict: never written to graph state
    (which the checkpointer persists), never touched by anything outside
    this module.
    """
    return {
        "configurable": {
            "thread_id": f"{tenant_id}:{session_id}",
            "tenant_id": tenant_id,
            "channel": channel,
            "called_number": called_number,
            "widget_key": widget_key,
            "tenant_config_override": tenant_config_override,
        },
        "recursion_limit": MAX_TOOL_HOPS * 2,
    }


async def thread_is_cold(tenant_id: str, session_id: str) -> bool:
    """True when the checkpointer has no history for this conversation.

    Happens on the first turn, and after a restart mid-call — which is exactly
    when a voice channel should reseed from the transcript it was sent.
    """
    try:
        snapshot = await get_graph().aget_state(thread_config(tenant_id, session_id))
    except Exception:  # a checkpointer hiccup must not kill the call
        logger.warning("could not read thread state for %s:%s", tenant_id, session_id)
        return False
    return not (snapshot and snapshot.values.get("messages"))


async def run_turn(**kwargs: Any) -> str:
    """Non-streaming convenience wrapper — tests and batch callers only."""
    reply = ""
    async for event in stream_turn(**kwargs):
        if event.type == "final":
            reply = event.text
        elif event.type == "error":
            raise RuntimeError(event.text)
    return reply


# --- helpers ----------------------------------------------------------------


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return "" if content is None else str(content)


def _messages(update: Any) -> Iterable[Any]:
    if isinstance(update, dict):
        return update.get("messages") or []
    return []


def _tool_calls(update: Any) -> list[dict[str, Any]]:
    for message in reversed(list(_messages(update))):
        if isinstance(message, AIMessage):
            return list(message.tool_calls or [])
    return []


def _handoff_artifact(message: Any) -> dict[str, Any] | None:
    """A `ToolMessage.artifact` signalling an escalation happened, or None.
    Read from the artifact (not the spoken text) so this stays independent of
    prompt wording — see `escalate` in `app/tools/messaging_tools.py`, which
    is the only tool that sets it.

    Emitted regardless of `artifact["transfer"]` — every channel gets to know
    an escalation happened; only voice ever turns it into a live Vapi
    transfer, and that decision belongs to `app/channels/vapi_llm.py`, which
    reads `transfer` out of `data` before emitting a `transferCall` frame.
    Filtering it out here (as this used to do) meant `channel="chat"` could
    never emit a `handoff` at all, since `SmsCallbackEscalator.can_transfer`
    is always False — the click-to-call affordance a widget wants was
    unreachable dead code.
    """
    artifact = getattr(message, "artifact", None)
    if isinstance(artifact, dict) and artifact.get("kind") == "handoff":
        return dict(artifact)
    return None


def _actions_artifact(message: Any) -> dict[str, Any] | None:
    """A `ToolMessage.artifact` carrying link/handoff buttons, or None.

    Set by `offer_actions` (`app/tools/action_tools.py`, Phase 9.1) — the
    model never sees a URL, only slugs; the artifact is what actually
    carries the resolved `{type, label, url, slug}` rows to a renderer.
    """
    artifact = getattr(message, "artifact", None)
    if isinstance(artifact, dict) and artifact.get("kind") == "actions":
        return dict(artifact)
    return None


def _cards_artifact(message: Any) -> dict[str, Any] | None:
    """A `ToolMessage.artifact` carrying a card carousel, or None.

    Set by `offer_cards` (`app/tools/card_tools.py`, Phase 9.2). Every URL
    inside has already been scheme- and host-checked by
    `app/flows/cards.py::sanitize_cards` — this only reads it.
    """
    artifact = getattr(message, "artifact", None)
    if isinstance(artifact, dict) and artifact.get("kind") == "cards":
        return dict(artifact)
    return None


def _flow_artifact(message: Any) -> dict[str, Any] | None:
    """A `ToolMessage.artifact` from `start_flow`, or None.

    Carries the scripted node's own text and buttons. Note this is also what
    `app/brain/graph.py::_after_tools` keys off to end the turn — the two
    read the same `kind`, so a flow can't render here and still fall back
    into `reason`.
    """
    artifact = getattr(message, "artifact", None)
    if isinstance(artifact, dict) and artifact.get("kind") == "flow":
        return dict(artifact)
    return None


def _auto_link_actions(urls: list[str]) -> list[dict[str, Any]]:
    """Build `offer_actions`-shaped entries for URLs the model wrote
    straight into its reply text (see the "actions" fallback in
    `stream_turn`) — the widget's `ActionButtons` renders these identically
    to a real catalog link, since the artifact shape is the same either
    way. Labelled by hostname since there's no operator-authored label to
    use, unlike a real `links` catalog entry."""
    return [
        {
            "type": "link",
            "label": _hostname_label(url),
            "url": url,
            "slug": f"auto-link-{index}",
        }
        for index, url in enumerate(urls, start=1)
    ]


def _hostname_label(url: str) -> str:
    host = urlparse(url).netloc or url
    return host.removeprefix("www.")


def _suggestions_artifact(message: Any) -> dict[str, Any] | None:
    """A `ToolMessage.artifact` carrying quick-reply candidates, or None.

    Set by `check_availability` (`app/tools/booking_tools.py`) so a widget can
    render slot chips without the model ever seeing — or being able to
    generate — UI markup. Never `is_spoken`: this is data for a renderer, not
    something to say aloud.
    """
    artifact = getattr(message, "artifact", None)
    if isinstance(artifact, dict) and artifact.get("kind") == "slots":
        return dict(artifact)
    return None
