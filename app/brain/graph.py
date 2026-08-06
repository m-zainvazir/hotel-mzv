"""The brain: one channel-agnostic LangGraph graph (plan §5).

    START → resolve_tenant → emergency_check → reason ⇄ tools → END

Voice and chat both enter here. If you ever find yourself adding a branch that
asks "is this Vapi?", it belongs in a channel adapter instead.
"""

from __future__ import annotations

import logging
import traceback
from urllib.parse import urlparse

from langchain_core.messages import AIMessage
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
    # `tools → reason` unconditionally from Phase 1 until 9.2. The one
    # exception is `start_flow`: a scripted flow node says exactly what its
    # config says and stops, so it must not hand back to the model at all.
    builder.add_conditional_edges("tools", _after_tools, {"reason": "reason", END: END})

    return builder.compile(checkpointer=checkpointer)


def _after_tools(state) -> str:
    """END after a flow node; back to `reason` for every other tool result.

    Termination has to be a graph edge rather than a prompt instruction.
    "Show this text and these buttons, then STOP" is something a model obeys
    *most* of the time — which is precisely the property a deterministic
    flow exists to remove. Phase 9.2's whole premise is that a `Main Menu`
    button is a certainty, and a trailing "Is there anything else I can help
    with?" bolted on by the model would quietly undo that.

    Reads the artifact rather than the text, like every other artifact
    consumer here (`app/brain/runner.py::_handoff_artifact` and friends), so
    it stays independent of `start_flow`'s wording.
    """
    for message in reversed(state.get("messages") or []):
        artifact = getattr(message, "artifact", None)
        if isinstance(artifact, dict) and artifact.get("kind") == "flow":
            return END
        # Only the tool results from this hop matter; stop at the AI message
        # that requested them so an earlier turn's flow can't strand the
        # graph forever.
        if isinstance(message, AIMessage):
            break
    return "reason"


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
