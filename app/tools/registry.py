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
from app.tools.messaging_tools import escalate, send_confirmation

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
#: emergency path is the worst dead air there is.
SLOW_TOOLS: frozenset[str] = frozenset(
    {"check_availability", "book_job", "send_confirmation", "escalate"}
)


def native_tools_for(tenant: TenantConfig, channel: str = "chat") -> list[BaseTool]:
    """The native tools this tenant may use on this channel.

    Kept as a function (not a constant) because Phase 4 tenants will enable
    different subsets, and because voice and chat won't always agree.
    """
    del tenant, channel  # every tenant gets the full critical path in Phase 1
    return list(NATIVE_TOOLS)


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
