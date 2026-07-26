"""Small shared helpers for turning tool results into model-readable text."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.tools.booking.base import Slot


def normalize_phone(raw: str) -> str | None:
    """Best-effort E.164. Returns None if it cannot possibly be a phone number."""
    if not raw:
        return None
    cleaned = raw.strip()
    plus = cleaned.startswith("+")
    digits = re.sub(r"\D", "", cleaned)

    if plus and 8 <= len(digits) <= 15:
        return f"+{digits}"
    if len(digits) == 10:  # bare US/CA number
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if 8 <= len(digits) <= 15:
        return f"+{digits}"
    return None


def parse_iso(value: str, tz: ZoneInfo) -> datetime | None:
    """Read an ISO-8601 string as the tenant's LOCAL wall-clock time.

    Any timezone offset or trailing 'Z' is deliberately DISCARDED — the
    wall-clock digits are re-stamped with the tenant's timezone. A
    receptionist's caller always means local time ("Saturday", "3pm" = at the
    business), and models routinely mis-encode a local date as UTC midnight:
    Gemini emitted `2026-08-01T00:00:00Z` for "next Saturday" at a New York
    hotel, and a naive UTC→local conversion turns that into Friday 8pm — a full
    day early, which is exactly the bug this prevents. The only two callers
    (`check_availability`/`book_job`) both parse model-supplied strings that
    represent local caller intent, and `book_job`'s `slot_start_iso` is copied
    from `check_availability`'s own already-local output, so discarding the
    offset is a no-op there and a correction everywhere else.
    """
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=tz)


def format_slots(slots: list[Slot], tz: ZoneInfo) -> str:
    """Number the options so the model and the caller can refer to them."""
    lines = []
    for index, slot in enumerate(slots, start=1):
        lines.append(f"{index}. {slot.label(tz)}  (slot_start_iso={slot.start.isoformat()})")
    return "\n".join(lines)


def speakable_datetime(moment: datetime, tz: ZoneInfo) -> str:
    local = moment.astimezone(tz)
    hour = local.hour % 12 or 12
    minute = f":{local.minute:02d}" if local.minute else ""
    meridiem = "am" if local.hour < 12 else "pm"
    return f"{local:%A} {local:%B} {local.day} at {hour}{minute}{meridiem}"
