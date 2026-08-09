"""The brain: one channel-agnostic LangGraph graph (plan §5).

    START → resolve_tenant → emergency_check → reason ⇄ tools → END

Voice and chat both enter here. If you ever find yourself adding a branch that
asks "is this Vapi?", it belongs in a channel adapter instead.
"""

from __future__ import annotations

import logging
import traceback
from urllib.parse import urlparse

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import tools_condition

from app.brain.nodes.emergency_check import emergency_check
from app.brain.nodes.reason import reason
from app.brain.nodes.resolve_tenant import resolve_tenant
from app.brain.nodes.tools import tools as tools_node
from app.brain.state import ReceptionistState
from app.config import get_settings

try:  # langgraph >= 0.3 renamed it; the old name stays as an alias for now
    from langgraph.checkpoint.memory import InMemorySaver as _MemorySaver
except ImportError:  # pragma: no cover
    from langgraph.checkpoint.memory import MemorySaver as _MemorySaver

logger = logging.getLogger(__name__)

_compiled = None


def _redact_password(text: str, database_url: str | None) -> str:
    """Replace every occurrence of `database_url`'s password with `***`."""
    if not database_url:
        return text
    password = urlparse(database_url).password
    if not password:
        return text
    return text.replace(password, "***")


def build_graph(checkpointer=None):
    """Compile the graph. `checkpointer=None` yields a stateless graph."""
    builder = StateGraph(ReceptionistState)

    builder.add_node("resolve_tenant", resolve_tenant)
    builder.add_node("emergency_check", emergency_check)
    builder.add_node("reason", reason)
    # Resolved per invocation, not once at compile time — `reason` binds a
    # per-tenant tool set (native + MCP), so `tools` must build ToolNode from
    # that same set on every call. See app/brain/nodes/tools.py's docstring.
    builder.add_node("tools", tools_node)

    builder.add_edge(START, "resolve_tenant")
    builder.add_edge("resolve_tenant", "emergency_check")
    builder.add_edge("emergency_check", "reason")
    builder.add_conditional_edges("reason", tools_condition, {"tools": "tools", END: END})
    # `tools → reason` unconditionally from Phase 1 until 9.2. Two exceptions
    # now, both for the same reason — the model has nothing left to say and
    # saying it anyway is worse than silence. See `_after_tools`.
    builder.add_conditional_edges("tools", _after_tools, {"reason": "reason", END: END})

    return builder.compile(checkpointer=checkpointer)


#: Tools whose entire job is to render something the model has *already*
#: introduced in words. Nothing follows them.
_PRESENTATION_ARTIFACTS = frozenset({"actions", "cards"})


def _after_tools(state) -> str:
    """END after a flow node or a pure presentation hop; else back to `reason`.

    Termination has to be a graph edge rather than a prompt instruction.
    "Show this text and these buttons, then STOP" is something a model obeys
    *most* of the time — which is precisely the property a deterministic
    flow exists to remove. Phase 9.2's whole premise is that a `Main Menu`
    button is a certainty, and a trailing "Is there anything else I can help
    with?" bolted on by the model would quietly undo that.

    **`offer_actions`/`offer_cards` need the same treatment, and this is the
    real cure for the cross-tool-hop restatement.** Measured live against
    production, every availability turn came back shaped like this:

        acknowledgement  "Let me check what we've got open."
        check_availability
        token            "...we don't have any spa availability this evening.
                          The earliest openings are Sunday, August 9th."
        offer_actions                       <- renders those slots as buttons
        token            "I'm afraid we don't have any spa openings for this
                          evening. The earliest is Sunday the 9th at 10am."

    The second paragraph is the defect users see. It exists because the
    unconditional `tools → reason` edge hands control back after a tool whose
    output is *the words that were just spoken, as buttons* — so the model,
    given a turn, restates. `RepeatSuppressor` cannot fix this: the wording
    differs every time, and text-level similarity can't distinguish "said
    this already" from "adding detail" without guessing (guessing wrong
    deletes the answer). Removing the turn removes the defect.

    Two guards, because ending a turn early is not free:

    * the hop must be *only* presentation tools — a batch that also called
      `check_availability` still owes the caller its result;
    * the model must have already spoken in the same message, or ending here
      would leave a turn that is buttons and no words.

    Reads artifacts rather than tool names, like every other artifact consumer
    here (`app/brain/runner.py::_handoff_artifact` and friends), so it stays
    independent of any tool's wording.
    """
    #: One entry per tool result in this hop — its artifact kind, or None for
    #: a tool that returns a plain string. Counting *results* rather than
    #: artifacts is what stops a mixed batch (`book_job` + `offer_actions`)
    #: from looking like a pure presentation hop: `book_job` has no artifact,
    #: so an artifact-only walk would never see it.
    kinds: list[str | None] = []
    for message in reversed(state.get("messages") or []):
        # Only the tool results from this hop matter; stop at the AI message
        # that requested them so an earlier turn's flow can't strand the
        # graph forever.
        if isinstance(message, AIMessage):
            spoke = bool(_as_text(message.content).strip())
            if kinds and spoke and all(kind in _PRESENTATION_ARTIFACTS for kind in kinds):
                return END
            break
        if isinstance(message, ToolMessage):
            artifact = getattr(message, "artifact", None)
            kind = artifact.get("kind") if isinstance(artifact, dict) else None
            if kind == "flow":
                return END
            kinds.append(kind)
    return "reason"


def _as_text(content) -> str:
    """AI message content is a string on most providers and a list of parts on
    some (Gemini, Anthropic) — only the text parts count as "the model spoke"."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content or "")


def get_graph():
    """Process-wide compiled graph. Starts on `InMemorySaver` — always safe,
    zero I/O, works before any Supabase config exists. `app/main.py`'s async
    lifespan calls `init_postgres_checkpointer()` right after this to swap in
    a durable checkpointer when one is configured (Phase 4 Step 7).
    """
    global _compiled
    if _compiled is None:
        _compiled = build_graph(checkpointer=_MemorySaver())
    return _compiled


async def init_postgres_checkpointer() -> None:
    """Swap the compiled graph onto a durable Postgres checkpointer, if
    `DATABASE_URL` is configured.

    Must run from an async context (`AsyncPostgresSaver.__init__` calls
    `asyncio.get_running_loop()`), which is why this can't happen inside the
    synchronous `get_graph()` — it's awaited once from `app/main.py`'s
    lifespan, after `get_graph()` has already seeded a safe in-memory
    fallback that nothing has served traffic on yet.

    Any failure here — the optional dependency missing, a bad connection, a
    `.setup()` error — logs a WARNING and leaves the in-memory graph in
    place. This feature degrading must never take down the app: conversation
    durability is a nice-to-have, not a reason to fail a boot.
    """
    settings = get_settings()
    if not settings.database_url:
        return

    from app.db.checkpointer import build_postgres_saver

    try:
        saver = await build_postgres_saver(settings.database_url)
    except Exception:
        # Not exc_info=True: some psycopg error paths embed the conninfo —
        # password included — in the exception's own message, and a raw
        # traceback would put that straight into the logs (Phase 7 Step 5).
        # Scrub the one secret this URL can contain before logging anything.
        detail = _redact_password(traceback.format_exc(), settings.database_url)
        logger.warning(
            "could not start the Postgres checkpointer — falling back to "
            "InMemorySaver; conversations will not survive a restart\n%s",
            detail,
        )
        return

    global _compiled
    _compiled = build_graph(checkpointer=saver)


def active_checkpointer_name() -> str:
    """`"postgres"` or `"memory"` — for GET /health."""
    return "memory" if isinstance(get_graph().checkpointer, _MemorySaver) else "postgres"


def reset_graph() -> None:
    """Drop the compiled graph (tests, or after swapping the LLM)."""
    global _compiled
    _compiled = None


def studio_graph(config=None):
    """Entry point for `langgraph dev` / LangGraph Studio (see langgraph.json).

    Compiled without a checkpointer because the dev server supplies its own
    persistence. The `config` argument is what the CLI passes in and is
    deliberately ignored — tenant scoping comes from `configurable.tenant_id`,
    which you set in the Studio input panel.
    """
    del config
    return build_graph()
