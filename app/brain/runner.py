"""Streaming runner — the one place channels talk to the brain.

Every adapter (terminal CLI, `/chat` SSE, Vapi `/chat/completions`) consumes
this async iterator and re-encodes it for its own transport. That's what keeps
business logic out of the adapters.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from app.brain.acknowledge import acknowledgement_for
from app.brain.graph import get_graph
from app.brain.metrics import TurnCounter
from app.brain.sanitize import InlineToolCallFilter, RepeatSuppressor
from app.tenancy.loader import resolve_tenant_id
from app.tools.registry import is_slow_tool

logger = logging.getLogger(__name__)

EventType = Literal[
    "token",
    "acknowledgement",
    "tool_start",
    "tool_result",
    "suggestions",
    "handoff",
    "final",
    "error",
]

MAX_TOOL_HOPS = 12


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
) -> AsyncIterator[BrainEvent]:
    """Run one conversational turn, yielding events as they happen.

    `history` seeds a *cold* thread and nothing else. Voice orchestrators resend
    the whole transcript every turn while our checkpointer already holds it, so
    passing history on a warm thread would double every message. The caller is
    responsible for checking `thread_is_cold()` first.
    """
    resolved = resolve_tenant_id(
        tenant_id=tenant_id,
        assistant_id=assistant_id,
        phone_number=called_number,
        widget_key=widget_key,
    )

    config = thread_config(
        resolved, session_id, channel=channel, called_number=called_number, widget_key=widget_key
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
    except Exception as exc:  # a channel must always get a terminal event
        logger.exception("brain turn failed tenant=%s session=%s", resolved, session_id)
        yield BrainEvent("error", str(exc))
        return

    if counter.requests > 2:
        logger.info("turn used %d llm requests (1 + tool hops + retries)", counter.requests)
    yield BrainEvent("final", "".join(spoken), data={"llm_requests": counter.requests})


def thread_config(
    tenant_id: str,
    session_id: str,
    *,
    channel: str = "chat",
    called_number: str | None = None,
    widget_key: str | None = None,
) -> dict[str, Any]:
    """The RunnableConfig for one conversation.

    Thread ids are tenant-prefixed so two tenants can never share conversation
    state, even if a session or call id collides.
    """
    return {
        "configurable": {
            "thread_id": f"{tenant_id}:{session_id}",
            "tenant_id": tenant_id,
            "channel": channel,
            "called_number": called_number,
            "widget_key": widget_key,
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
