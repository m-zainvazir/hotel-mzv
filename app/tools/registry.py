"""Tool registry — the two-tier rule made concrete (CLAUDE.md convention #2).

Tier 1 (here): native, typed, validated tools on the critical path.
Tier 2 (app/mcp): long-tail integrations, loaded per tenant, kept off the
first-response path.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from app.tenancy.models import TenantConfig
from app.tools.booking_tools import book_job, check_availability
from app.tools.emergency_tools import is_emergency
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

#: Tools that hit the network / a calendar and therefore must be preceded by a
#: spoken acknowledgement rather than silence. `escalate` now does real
#: network I/O (SMS, and on voice a live transfer) — dead air on the
#: emergency path is the worst dead air there is. `search_knowledge` joins
#: this list even though `is_slow_tool` below would already treat it as slow
#: via the "anything not in the fixed five" fallback (the same mechanism
#: that covers every MCP tool) — explicit here because it's a known,
#: named tool, not an arbitrary long-tail one.
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
    """
    del channel  # every tenant gets the full critical path regardless of channel
    tools = list(NATIVE_TOOLS)
    if tenant.knowledge.enabled:
        tools.append(search_knowledge)
    return tools


def is_slow_tool(name: str) -> bool:
    """True for any tool that must be preceded by a spoken acknowledgement.

    Every MCP tool is off the fast path by definition (plan §11's "long
    tail") — a third-party server thinking is exactly the dead-air case
    `SLOW_TOOLS` exists to prevent, and there's no way to enumerate MCP tool
    names up front the way `SLOW_TOOLS` does for the fixed native five. So
    the rule inverts: anything that *isn't* a known-fast native tool counts
    as slow.
    """
    return name in SLOW_TOOLS or name not in NATIVE_TOOLS_BY_NAME
