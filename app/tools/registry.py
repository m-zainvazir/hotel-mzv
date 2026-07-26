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
