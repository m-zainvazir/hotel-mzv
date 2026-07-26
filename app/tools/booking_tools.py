"""Native booking tools — tier 1 (critical path).

Typed, validated, fast. Errors come back as `ERROR: ...` strings rather than
exceptions so the model can recover inside the same conversation instead of
the turn blowing up on the caller.
"""

from __future__ import annotations

import logging
from datetime import datetime

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.tools.booking.base import BookingError, BookingRequest, SlotUnavailableError
from app.tools.context import channel_from_config, tenant_from_config
from app.tools.formatting import format_slots, normalize_phone, parse_iso, speakable_datetime
from app.tools.providers import get_booking_provider

logger = logging.getLogger(__name__)

#: How many candidate slots to pull before narrowing to the ones nearest the
#: caller's requested time. Comfortably covers a full day of fine-grained slots
#: so "as late as possible" can reach the last slot of the day, not just the
#: first few after the floor.
_CANDIDATE_LIMIT = 96


@tool(response_format="content_and_artifact")
async def check_availability(
    service: str,
    earliest_iso: str = "",
    config: RunnableConfig = None,
) -> tuple[str, dict]:
    """Find the next available appointment slots for a service.

    Call this before offering any time to the caller — never invent availability.

    `service` is what the caller asked for in their own words (e.g. "panel
    upgrade", "my lights keep flickering"); it is matched against the business's
    service catalogue.

    `earliest_iso` is the time the caller is interested in ("Saturday", "around
    6pm", "as late as you have", "midnight"). Give it in the business's LOCAL
    time as plain ISO-8601 with NO timezone suffix — e.g. `2026-08-01T21:00:00`
    for Saturday night, `2026-08-01T00:00:00` for the start of Saturday. Never
    append a 'Z' or an offset. The tool returns the open slots CLOSEST to that
    time (some may be a little earlier, some later), so ALWAYS call it even when
    the requested time sounds outside opening hours — you'll get the nearest
    real options to offer ("the latest we have that evening is 9:30") instead of
    just telling the caller no. Leave it empty for the soonest available.

    Returns a numbered list of options, each with the `slot_start_iso` value to
    pass to `book_job`.
    """
    tenant = tenant_from_config(config)
    matched = tenant.find_service(service)
    if matched is None:
        catalogue = ", ".join(s.name for s in tenant.services)
        return f"ERROR: no service matches {service!r}. Available services: {catalogue}", {}

    preferred = parse_iso(earliest_iso, tenant.tz) if earliest_iso else None
    if earliest_iso and preferred is None:
        return (
            f"ERROR: could not parse earliest_iso={earliest_iso!r}. Use local ISO-8601.",
            {},
        )

    # Search from the START of the caller's requested day rather than the exact
    # time, so slots a little EARLIER the same day are candidates too. That is
    # what lets "anything late?" surface the nearest evening slot instead of
    # only ever the first slot after the floor. Never earlier than now.
    day_floor = None
    if preferred is not None:
        day_start = preferred.replace(hour=0, minute=0, second=0, microsecond=0)
        day_floor = max(day_start, datetime.now(tenant.tz))

    provider = get_booking_provider(tenant)
    try:
        slots = await provider.check_availability(
            tenant, matched, earliest=day_floor, limit=_CANDIDATE_LIMIT
        )
    except BookingError:
        logger.exception(
            "check_availability failed tenant=%s service=%s", tenant.tenant_id, matched.slug
        )
        return (
            "ERROR: the calendar is not responding right now. Apologise, and offer to take "
            "the caller's details for a callback instead."
        ), {}

    if not slots:
        return (
            f"No {matched.name} slots free in the next {tenant.booking.horizon_days} days. "
            "Offer to take the caller's details for a callback."
        ), {}

    keep = tenant.booking.max_slots_returned
    if preferred is not None:
        # Nearest to what they asked for, in either direction; then show them
        # in chronological order so the caller hears "6, 6:30, 7" naturally.
        slots = sorted(slots, key=lambda s: abs(s.start - preferred))[:keep]
        slots.sort(key=lambda s: s.start)
        header = "closest available to that time"
    else:
        slots = slots[:keep]
        header = "next available"

    text = (
        f"{matched.name} ({matched.duration_minutes} min"
        + (f", ${matched.price_usd:.0f}" if matched.price_usd else "")
        + f") — {header}:\n{format_slots(slots, tenant.tz)}\n"
        f"Offer these to the caller, then call book_job with the chosen slot_start_iso."
    )
    # A widget can render these as quick-reply chips (Phase 5) — the model
    # never sees this, so it can't be coaxed into emitting UI markup as text
    # (the exact failure app/brain/sanitize.py exists to clean up elsewhere).
    artifact = {
        "kind": "slots",
        "service": matched.slug,
        "slots": [
            {"start_iso": slot.start.isoformat(), "label": slot.label(tenant.tz)} for slot in slots
        ],
    }
    return text, artifact


@tool
async def book_job(
    service: str,
    slot_start_iso: str,
    customer_name: str,
    customer_phone: str,
    address: str = "",
    customer_email: str = "",
    notes: str = "",
    config: RunnableConfig = None,
) -> str:
    """Book a confirmed appointment. Only call this once you have every argument.

    `slot_start_iso` must be copied VERBATIM from a `check_availability` result
    (it is already in the business's local time — do not rewrite it, add a 'Z',
    or convert it to UTC). `address` is where the work happens — only ask for it
    if the business needs one.
    `customer_phone` is the number to text the confirmation to. `customer_email`
    is optional; only pass it if the caller volunteers one. `notes` carries
    anything the technician should know.

    Returns the job id — pass it to `send_confirmation`.
    """
    tenant = tenant_from_config(config)
    channel = channel_from_config(config)

    matched = tenant.find_service(service)
    if matched is None:
        catalogue = ", ".join(s.name for s in tenant.services)
        return f"ERROR: no service matches {service!r}. Available services: {catalogue}"

    start = parse_iso(slot_start_iso, tenant.tz)
    if start is None:
        return f"ERROR: could not parse slot_start_iso={slot_start_iso!r}. Use ISO-8601."

    phone = normalize_phone(customer_phone)
    if phone is None:
        return "ERROR: customer_phone is not a usable phone number. Ask the caller to repeat it."
    if not customer_name.strip():
        return "ERROR: customer_name is required. Ask the caller for their name."
    if tenant.booking.require_address and not address.strip():
        return "ERROR: address is required. Ask the caller where the work is."

    provider = get_booking_provider(tenant)
    try:
        job = await provider.create_booking(
            tenant,
            BookingRequest(
                service_slug=matched.slug,
                start=start,
                customer_name=customer_name.strip(),
                customer_phone=phone,
                address=address.strip(),
                customer_email=customer_email.strip(),
                notes=notes.strip() or None,
                channel=channel,
            ),
        )
    except SlotUnavailableError as exc:
        return f"ERROR: {exc}. Call check_availability again and offer new times."
    except BookingError:
        logger.exception("booking failed tenant=%s service=%s", tenant.tenant_id, matched.slug)
        return (
            "ERROR: the calendar is not responding. Apologise, take the caller's number, and "
            "tell them someone will call back to confirm. Do NOT say it is booked."
        )
    except Exception:
        logger.exception(
            "unexpected booking failure tenant=%s service=%s", tenant.tenant_id, matched.slug
        )
        return (
            "ERROR: could not complete the booking. Offer a callback instead. "
            "Do NOT say it is booked."
        )

    logger.info("booked job=%s tenant=%s service=%s", job.id, tenant.tenant_id, job.service_slug)
    where = f" at {job.address}" if job.address else ""
    return (
        f"BOOKED job_id={job.id} — {job.service_name} for {job.customer_name} on "
        f"{speakable_datetime(job.scheduled_start, tenant.tz)}{where}. "
        f"Now call send_confirmation with job_id={job.id}, then tell the caller it's confirmed."
    )
