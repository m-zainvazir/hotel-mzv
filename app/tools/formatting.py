"""Small shared helpers for turning tool results into model-readable text."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.tools.booking.base import Slot

#: Enough of a map to cover the timezones this product is actually deployed
#: in, so a tenant that never sets `dial_code` still gets its own country.
#: Deliberately small and explicit: a full IANA-zone-to-country table is a
#: dependency, and a wrong entry here is a phone number nobody can call.
_TIMEZONE_DIAL_CODES = {
    "Asia/Karachi": "+92",
    "Asia/Dubai": "+971",
    "Asia/Riyadh": "+966",
    "Asia/Kolkata": "+91",
    "Asia/Calcutta": "+91",
    "Europe/London": "+44",
    "Europe/Dublin": "+353",
    "Australia/Sydney": "+61",
    "Australia/Melbourne": "+61",
    "Pacific/Auckland": "+64",
}


def dial_code_for(tenant) -> str | None:
    """The tenant's own country code: configured, else inferred from its
    timezone, else `None` — which means "don't guess a country"."""
    configured = getattr(tenant, "dial_code", None)
    if configured:
        return configured
    zone = getattr(tenant, "timezone", "") or ""
    if zone.startswith("America/"):
        return "+1"
    return _TIMEZONE_DIAL_CODES.get(zone)


def normalize_phone(raw: str, dial_code: str | None = None) -> str | None:
    """Best-effort E.164. Returns None if it cannot possibly be a phone number.

    `dial_code` is the business's own country code (`"+92"`), from
    `TenantConfig.dial_code`. It matters most for a **national** number — one
    written with the local trunk prefix, like `0333 3333333` — because the
    leading zero is not part of the international number and the country has
    to come from somewhere.

    That somewhere used to be a hardcoded `+1`: any ten-digit input was
    assumed to be US/Canadian. A Pakistani caller reading out `0333333333`
    was stored as `+10333333333` — a number nobody can ring back, produced
    silently, on a tenant whose timezone said `Asia/Karachi` in the very
    same config. Guessing a country is worse than not knowing one: without a
    dial code this now keeps the digits as given rather than inventing a
    country for them.
    """
    if not raw:
        return None
    cleaned = raw.strip()
    plus = cleaned.startswith("+")
    digits = re.sub(r"\D", "", cleaned)

    if plus and 8 <= len(digits) <= 15:
        return f"+{digits}"

    code = (dial_code or "").strip()
    code_digits = re.sub(r"\D", "", code)

    if code_digits:
        if digits.startswith("0"):
            # National format: the trunk prefix is replaced by the country
            # code, never appended to it.
            national = digits.lstrip("0")
            if 6 <= len(national) <= 14:
                return f"+{code_digits}{national}"
        if digits.startswith(code_digits) and 8 <= len(digits) <= 15:
            return f"+{digits}"  # already international, just missing the +
        if 6 <= len(digits) <= 11:
            return f"+{code_digits}{digits}"

    if digits.startswith("0") or len(digits) == 10:
        # A national number with no country to attach it to. `+0…` is not a
        # thing — E.164 numbers never begin with zero — and prefixing a bare
        # ten-digit number with nothing produces `+5551112222`, which claims
        # a country code of 555. Honestly unusable beats quietly malformed:
        # pass the tenant's `dial_code` and this resolves properly.
        return None
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
