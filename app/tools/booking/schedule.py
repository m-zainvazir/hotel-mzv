"""Opening hours, resolved once per tenant and cached (Phase 9.4).

`BookingProvider.availability_schedule` is allowed to make a real network
call. `reason` renders the system prompt on **every turn**, so calling it
directly there would put a Cal.com round trip on the §13 latency budget for
a value that changes maybe twice a year.

Two callers, one cache:

* `app/brain/nodes/reason.py` — one line of the system prompt.
* `app/channels/admin.py` — the Config tab's read-only "managed in Cal.com"
  panel.

Both want the same answer, and the panel refreshing it is what makes an edit
in Cal.com show up in the bot without waiting out the TTL.

Nothing here raises. A calendar outage must cost the bot its hours line, not
the turn — the same posture `_record_chat_message` and `load_mcp_tools`
already take.
"""

from __future__ import annotations

import logging
import time
from threading import RLock

from app.config import get_settings
from app.tenancy.models import TenantConfig
from app.tools.booking.base import AvailabilitySchedule
from app.tools.providers import get_booking_provider

logger = logging.getLogger(__name__)

#: tenant_id -> (fingerprint, expires_at, schedule or None). A cached *miss*
#: is cached too: a tenant whose provider has no schedule would otherwise pay
#: a failed lookup on every single turn.
_cache: dict[str, tuple[str, float, AvailabilitySchedule | None]] = {}
_lock = RLock()


def _fingerprint(tenant: TenantConfig) -> str:
    """Everything that could change which schedule is the right answer.

    Keyed on more than `tenant_id` for the same reason the MCP tool cache is
    (`app/mcp/client.py`): a config edit — switching provider, pointing at a
    different event type, moving timezone, or editing the manual grid on a
    stub tenant — must invalidate immediately rather than serve a stale
    answer for the rest of the TTL. The admin panel's whole promise is that
    an edit lands on the next turn.
    """
    booking = tenant.booking
    hours = tenant.hours_summary() if booking.provider == "stub" else ""
    return f"{booking.provider}:{booking.event_type_id}:{tenant.timezone}:{hours}"


async def availability_schedule_for(
    tenant: TenantConfig, *, refresh: bool = False
) -> AvailabilitySchedule | None:
    """This tenant's opening hours, or None if nothing can state them."""
    fingerprint = _fingerprint(tenant)
    now = time.monotonic()

    if not refresh:
        with _lock:
            cached = _cache.get(tenant.tenant_id)
        if cached and cached[0] == fingerprint and cached[1] > now:
            return cached[2]

    schedule: AvailabilitySchedule | None = None
    try:
        schedule = await get_booking_provider(tenant).availability_schedule(tenant)
    except Exception as exc:  # noqa: BLE001 — a prompt line is never worth a failed turn
        # `type(exc).__name__` alongside the message: str() of an httpx
        # timeout is the empty string, which once made a snapshot failure
        # log "failed wholesale: " and name nothing (CLAUDE.md).
        logger.warning(
            "could not read the schedule for tenant=%s: %s: %s",
            tenant.tenant_id,
            type(exc).__name__,
            exc,
        )
        # Deliberately still cached, as a miss — a calendar that is down
        # stays down for more than one turn, and retrying every turn would
        # add a timeout's worth of latency to each one.

    ttl = get_settings().booking_schedule_cache_seconds
    with _lock:
        _cache[tenant.tenant_id] = (fingerprint, now + ttl, schedule)
    return schedule


async def business_hours_for(tenant: TenantConfig) -> str | None:
    """The one-line form the system prompt uses. None means "can't say"."""
    schedule = await availability_schedule_for(tenant)
    return schedule.summary() if schedule else None


def clear_schedule_cache() -> None:
    """Test hook, and used by the admin panel after a config save."""
    with _lock:
        _cache.clear()
