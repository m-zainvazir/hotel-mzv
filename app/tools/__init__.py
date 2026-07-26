"""Native (tier-1) tools and the provider seams behind them."""

from app.tools.booking_tools import book_job, check_availability
from app.tools.emergency import EmergencyVerdict, classify_emergency
from app.tools.emergency_tools import is_emergency
from app.tools.messaging_tools import escalate, send_confirmation
from app.tools.registry import (
    NATIVE_TOOLS,
    NATIVE_TOOLS_BY_NAME,
    SLOW_TOOLS,
    native_tools_for,
)

__all__ = [
    "NATIVE_TOOLS",
    "NATIVE_TOOLS_BY_NAME",
    "SLOW_TOOLS",
    "EmergencyVerdict",
    "book_job",
    "check_availability",
    "classify_emergency",
    "escalate",
    "is_emergency",
    "native_tools_for",
    "send_confirmation",
]
