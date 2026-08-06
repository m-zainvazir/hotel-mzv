"""Tool registry — the two-tier rule made concrete (CLAUDE.md convention #2).

Tier 1 (here): native, typed, validated tools on the critical path.
Tier 2 (app/mcp): long-tail integrations, loaded per tenant, kept off the
first-response path.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from app.tenancy.models import TenantConfig
from app.tools.action_tools import offer_actions
from app.tools.booking_tools import book_job, check_availability
from app.tools.card_tools import offer_cards
from app.tools.emergency_tools import is_emergency
from app.tools.flow_tools import start_flow
from app.tools.knowledge_tools import search_knowledge
from app.tools.messaging_tools import escalate, send_confirmation

#: The unconditional critical path — every tenant gets exactly these five,
#: always. Deliberately NOT where `search_knowledge` lives (Phase 9 Part C):
#: it's a sixth native tool, but a *conditional* one, bound only for tenants
#: with a knowledge base — see `native_tools_for` below. Keeping this
#: constant fixed at five is what keeps `test_critical_path_tools_are_all_native`
#: (tests/test_native_tools.py) meaningful as a "these never change" guard.
NATIVE_TOOLS: list[BaseTool] = [
    check_availability,
    book_job,
    send_confirmation,
    escalate,
    is_emergency,
]

NATIVE_TOOLS_BY_NAME: dict[str, BaseTool] = {t.name: t for t in NATIVE_TOOLS}

#: Every native tool that exists, including the conditional ones no tenant
#: is guaranteed to have. Distinct from `NATIVE_TOOLS` above, which is
#: deliberately frozen at the unconditional five.
#:
#: This exists because `is_slow_tool` had a real bug from Phase 9 Part C
#: onward: it inverted the rule as "anything not in the fixed five is slow",
#: which is right for MCP tools (the case it was written for) and wrong for
#: every *conditional native* one. `offer_actions` has been triggering a
#: spoken "bear with me a second…" before an instant in-memory dict lookup
#: since 9.1, with its own docstring stating the opposite. Harmless-looking
#: in chat; on a flow node it's worse than cosmetic, since a deterministic
#: node's configured wording would arrive with a model-ish phrase glued to
#: the front of it — the one thing the whole feature promises won't happen.
ALL_NATIVE_TOOLS: list[BaseTool] = [
    *NATIVE_TOOLS,
    search_knowledge,
    offer_actions,
    start_flow,
    offer_cards,
]

_ALL_NATIVE_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in ALL_NATIVE_TOOLS)

#: Tools that hit the network / a calendar and therefore must be preceded by a
#: spoken acknowledgement rather than silence. `escalate` now does real
#: network I/O (SMS, and on voice a live transfer) — dead air on the
#: emergency path is the worst dead air there is. `search_knowledge` joins
#: this list even though `is_slow_tool` below would already treat it as slow
#: via the "anything not in the fixed five" fallback (the same mechanism
#: that covers every MCP tool) — explicit here because it's a known,
#: named tool, not an arbitrary long-tail one.
#: `offer_actions` (Phase 9.1), `start_flow` and `offer_cards` (Phase 9.2)
#: are deliberately NOT here — they're pure in-memory config lookups and
#: transforms, so an acknowledgement before one would be a pointless stall
#: for something that never touches the network. Whatever *produced* a
#: card's data (an MCP search, usually) is already covered by `is_slow_tool`'s
#: "anything not in the fixed five" fallback, so the acknowledgement still
#: fires before the slow part.
SLOW_TOOLS: frozenset[str] = frozenset(
    {"check_availability", "book_job", "send_confirmation", "escalate", "search_knowledge"}
)


def native_tools_for(tenant: TenantConfig, channel: str = "chat") -> list[BaseTool]:
    """The native tools this tenant may use on this channel.

    Kept as a function (not a constant) because Phase 4 tenants will enable
    different subsets, and because voice and chat won't always agree. Phase 9
    Part C is the first tenant this seam actually branches on:
    `search_knowledge` is appended only when `tenant.knowledge.enabled`, so a
    bot with no knowledge base never carries its schema in the prompt. Called
    from two places that must stay in agreement — `app/brain/nodes/reason.py`
    (binds tools) and `app/brain/nodes/tools.py` (executes them) — putting the
    branch here, in the one function both call, is what keeps them in sync
    automatically rather than requiring two edits kept in sync by hand.

    Phase 9.1's `offer_actions` is the FIRST time `channel` itself is read
    (it's been `del channel` since Phase 1, with a docstring promising
    exactly this) — bound only on `channel == "chat"`, since a voice caller
    can't click a button and binding it there would only invite the model to
    read a URL aloud. Phase 9.2's `start_flow` and `offer_cards` are gated
    the same way and for the same reason: a carousel and a button menu are
    both meaningless down a phone line.
    """
    tools = list(NATIVE_TOOLS)
    if tenant.knowledge.enabled:
        tools.append(search_knowledge)
    if channel == "chat":
        # Unconditional on chat, unlike every other conditional tool here.
        # `offer_actions` and `offer_cards` are how this channel *presents*
        # things at all, and the project's goal is that an operator writes
        # an AI prompt and nothing else — gating them on config would mean
        # a bot could never build a button its operator hadn't already
        # typed out, which is the opposite of that. `ui.buttons` /
        # `ui.cards` are kill switches read inside the tools themselves
        # rather than binding gates, so a bot that has them switched off
        # can still be *told* about them without the schema vanishing
        # mid-conversation.
        tools.append(offer_actions)
        tools.append(offer_cards)
        # `start_flow` stays conditional: a flow is by definition something
        # an operator declared, so a tenant with none has nothing to start.
        if tenant.flows:
            tools.append(start_flow)
    return tools


def is_slow_tool(name: str) -> bool:
    """True for any tool that must be preceded by a spoken acknowledgement.

    Every MCP tool is off the fast path by definition (plan §11's "long
    tail") — a third-party server thinking is exactly the dead-air case
    `SLOW_TOOLS` exists to prevent, and there's no way to enumerate MCP tool
    names up front the way `SLOW_TOOLS` does for native ones. So the rule
    inverts: anything that *isn't* a known native tool counts as slow.

    "Known native" means `ALL_NATIVE_TOOLS`, not `NATIVE_TOOLS` — see that
    constant for the bug this distinction fixes.
    """
    return name in SLOW_TOOLS or name not in _ALL_NATIVE_TOOL_NAMES
