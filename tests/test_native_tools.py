"""The five tier-1 tools, invoked exactly as the graph invokes them."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from app.db.memory_store import get_store
from app.tools.booking_tools import book_job, check_availability
from app.tools.context import MissingTenantContextError
from app.tools.emergency_tools import is_emergency
from app.tools.formatting import normalize_phone, parse_iso
from app.tools.messaging_tools import escalate, send_confirmation
from app.tools.registry import NATIVE_TOOLS, SLOW_TOOLS
from tests.conftest import tool_config


def _offered_starts(result: str, tz):
    return [parse_iso(iso, tz) for iso in re.findall(r"slot_start_iso=([^)\s]+)", result)]


def test_critical_path_tools_are_all_native():
    """CLAUDE.md convention #2 — these five are tier 1, not MCP. Unchanged by
    Phase 9 Part C: `search_knowledge` is a sixth native tool, but a
    *conditional* one (bound only for tenants with a knowledge base, via
    `native_tools_for` — see test_knowledge_tool.py), never part of this
    fixed, unconditional five."""
    assert {t.name for t in NATIVE_TOOLS} == {
        "check_availability",
        "book_job",
        "send_confirmation",
        "escalate",
        "is_emergency",
    }
    assert SLOW_TOOLS <= {t.name for t in NATIVE_TOOLS} | {"search_knowledge"}


async def test_check_availability_returns_bookable_slots(hotel):
    result = await check_availability.ainvoke(
        {"service": "book a room"}, config=tool_config(hotel.tenant_id)
    )
    assert "slot_start_iso=" in result
    assert "Room reservation" in result


async def test_check_availability_rejects_unknown_service(hotel):
    result = await check_availability.ainvoke(
        {"service": "roof repair"}, config=tool_config(hotel.tenant_id)
    )
    assert result.startswith("ERROR:")
    assert "Event space" in result  # tells the model what it *can* book


async def test_check_availability_rejects_unparseable_date(hotel):
    result = await check_availability.ainvoke(
        {"service": "event space", "earliest_iso": "next tuesdayish"},
        config=tool_config(hotel.tenant_id),
    )
    assert result.startswith("ERROR:")


async def test_a_late_request_offers_the_nearest_evening_slots_not_the_morning(hotel):
    """The whole point of the "nearest" change: asking for a late time must
    surface the LAST slots of the day (bracketing the request), not the first
    slots after opening. hotel-mzv runs 7am-10pm, so a 9pm request should land
    on ~8:30-9:30pm, never 7am."""
    target = (datetime.now(hotel.tz) + timedelta(days=3)).replace(
        hour=21, minute=0, second=0, microsecond=0
    )
    result = await check_availability.ainvoke(
        {"service": "room-reservation", "earliest_iso": target.strftime("%Y-%m-%dT%H:%M:%S")},
        config=tool_config(hotel.tenant_id),
    )
    starts = _offered_starts(result, hotel.tz)
    assert starts, result
    assert all(abs((s - target).total_seconds()) <= 3600 for s in starts), result
    assert any(s >= target for s in starts), result  # includes at-or-after the request
    assert all(s.hour >= 19 for s in starts), result  # evening, never the 7am block


async def test_a_pre_opening_request_offers_the_earliest_morning_slots(hotel):
    """Symmetric to the late case: a time before opening returns the first
    slots of the day, which is how "our earliest is 7am" already worked."""
    target = (datetime.now(hotel.tz) + timedelta(days=3)).replace(
        hour=5, minute=0, second=0, microsecond=0
    )
    result = await check_availability.ainvoke(
        {"service": "room-reservation", "earliest_iso": target.strftime("%Y-%m-%dT%H:%M:%S")},
        config=tool_config(hotel.tenant_id),
    )
    starts = _offered_starts(result, hotel.tz)
    assert starts, result
    assert min(starts).hour == 7, result  # 5am is before open; nearest is the 7am block


async def _first_slot_iso(tenant) -> str:
    listing = await check_availability.ainvoke(
        {"service": "room-reservation"}, config=tool_config(tenant.tenant_id)
    )
    return listing.split("slot_start_iso=")[1].split(")")[0]


async def test_book_job_creates_a_job(hotel):
    result = await book_job.ainvoke(
        {
            "service": "room-reservation",
            "slot_start_iso": await _first_slot_iso(hotel),
            "customer_name": "Dana Reyes",
            "customer_phone": "(555) 111-2222",
        },
        config=tool_config(hotel.tenant_id),
    )

    assert result.startswith("BOOKED job_id=")
    jobs = get_store().list_jobs(hotel.tenant_id)
    assert len(jobs) == 1
    assert jobs[0].customer_phone == "+15551112222"  # normalised on the way in
    assert jobs[0].channel == "chat"


@pytest.mark.parametrize(
    ("field", "value"),
    [("customer_phone", "12"), ("customer_name", "  ")],
)
async def test_book_job_validates_required_details(hotel, field, value):
    payload = {
        "service": "room-reservation",
        "slot_start_iso": await _first_slot_iso(hotel),
        "customer_name": "Dana",
        "customer_phone": "5551112222",
    }
    payload[field] = value

    result = await book_job.ainvoke(payload, config=tool_config(hotel.tenant_id))
    assert result.startswith("ERROR:")
    assert get_store().list_jobs(hotel.tenant_id) == []


async def test_book_job_validates_address_when_the_tenant_requires_one(northside):
    """hotel-mzv doesn't require an address (require_address=false); northside
    does — the validation itself is tested against a tenant that needs it."""
    listing = await check_availability.ainvoke(
        {"service": "drain-clearing"}, config=tool_config(northside.tenant_id)
    )
    slot_iso = listing.split("slot_start_iso=")[1].split(")")[0]

    result = await book_job.ainvoke(
        {
            "service": "drain-clearing",
            "slot_start_iso": slot_iso,
            "customer_name": "Dana",
            "customer_phone": "5551112222",
            "address": "",
        },
        config=tool_config(northside.tenant_id),
    )
    assert result.startswith("ERROR: address is required")
    assert get_store().list_jobs(northside.tenant_id) == []


async def test_book_job_reports_a_taken_slot_instead_of_raising(hotel):
    payload = {
        "service": "room-reservation",
        "slot_start_iso": await _first_slot_iso(hotel),
        "customer_name": "Dana",
        "customer_phone": "5551112222",
    }
    assert (await book_job.ainvoke(payload, config=tool_config(hotel.tenant_id))).startswith(
        "BOOKED"
    )

    second = await book_job.ainvoke(payload, config=tool_config(hotel.tenant_id))
    assert second.startswith("ERROR:")
    assert "check_availability" in second


async def test_send_confirmation_texts_the_customer(hotel):
    booked = await book_job.ainvoke(
        {
            "service": "room-reservation",
            "slot_start_iso": await _first_slot_iso(hotel),
            "customer_name": "Dana Reyes",
            "customer_phone": "5551112222",
        },
        config=tool_config(hotel.tenant_id),
    )
    job_id = booked.split("job_id=")[1].split()[0]

    result = await send_confirmation.ainvoke(
        {"job_id": job_id}, config=tool_config(hotel.tenant_id)
    )

    assert result.startswith("SENT confirmation to +15551112222")
    messages = get_store().list_messages(hotel.tenant_id)
    assert len(messages) == 1
    assert "Hotel_MZV" in messages[0].body


async def test_send_confirmation_rejects_unknown_job(hotel):
    result = await send_confirmation.ainvoke(
        {"job_id": "job_nope"}, config=tool_config(hotel.tenant_id)
    )
    assert result.startswith("ERROR:")


async def test_send_confirmation_store_error_says_booking_is_confirmed(hotel, monkeypatch):
    """A transient Supabase error looking up the job is NOT the same as
    "unknown job" — the booking almost certainly landed, so the model must
    never be invited to book again (Phase 4)."""
    import app.tools.messaging_tools as messaging_tools_module
    from app.db.supabase_store import SupabaseStoreError

    class _BrokenStore:
        async def aget(self, tenant_id: str, job_id: str):
            raise SupabaseStoreError("simulated 5xx")

    monkeypatch.setattr(messaging_tools_module, "get_store", lambda: _BrokenStore())

    result = await send_confirmation.ainvoke(
        {"job_id": "job_whatever"}, config=tool_config(hotel.tenant_id)
    )

    assert result.startswith("ERROR:")
    assert "IS confirmed" in result
    assert "Do not book again" in result


async def test_escalate_records_and_alerts_on_call(hotel):
    result = await escalate.ainvoke(
        {"reason": "smoke in the corridor", "caller_summary": "Dana, room 214"},
        config=tool_config(hotel.tenant_id, channel="voice"),
    )

    assert result.startswith("ESCALATED")

    escalations = get_store().list_escalations(hotel.tenant_id)
    assert len(escalations) == 1
    assert escalations[0].transferred_to == hotel.emergency.escalation_phone

    alerts = [m for m in get_store().list_messages(hotel.tenant_id) if m.kind == "alert"]
    assert len(alerts) == 1
    assert "smoke in the corridor" in alerts[0].body


async def test_escalate_never_promises_a_transfer_on_chat(hotel):
    """Chat has no live transfer, ever — saying "putting you through" would
    be a lie there, and on the emergency path it's the most dangerous kind."""
    result = await escalate.ainvoke(
        {"reason": "smoke in the corridor"}, config=tool_config(hotel.tenant_id, channel="chat")
    )

    assert "Transferring" not in result
    assert "CANNOT transfer" in result
    assert hotel.emergency.escalation_phone in result


async def test_escalate_promises_a_transfer_on_voice_by_default(hotel, override_tenant):
    """Phase 3: voice gets a real WarmTransferEscalator by default (as long as
    the tenant has an escalation_phone and hasn't opted out), and the wording
    follows automatically from `Escalator.can_transfer`.

    hotel-mzv.json itself has `allow_warm_transfer: false` (no real
    escalation number provisioned yet — plans/phase7.md Step 11), so this
    registers an explicit override via `override_tenant` rather than relying
    on the fixture's current real-world value."""
    tenant = hotel.model_copy(
        update={"emergency": hotel.emergency.model_copy(update={"allow_warm_transfer": True})}
    )
    override_tenant(tenant)

    result = await escalate.ainvoke(
        {"reason": "gas smell"}, config=tool_config(tenant.tenant_id, channel="voice")
    )

    assert "Transferring the caller" in result
    assert "CANNOT transfer" not in result
    assert tenant.emergency.escalation_phone in result


async def test_allow_warm_transfer_false_keeps_voice_on_the_sms_path(hotel, override_tenant):
    tenant = hotel.model_copy(
        update={"emergency": hotel.emergency.model_copy(update={"allow_warm_transfer": False})}
    )
    override_tenant(tenant)

    result = await escalate.ainvoke(
        {"reason": "gas smell"}, config=tool_config(tenant.tenant_id, channel="voice")
    )

    assert "CANNOT transfer" in result
    assert "Transferring" not in result


async def test_is_emergency_tool_matches_the_node(hotel):
    dangerous = await is_emergency.ainvoke(
        {"utterance": "there's smoke in the corridor"}, config=tool_config(hotel.tenant_id)
    )
    routine = await is_emergency.ainvoke(
        {"utterance": "I'd like to book a spa treatment"}, config=tool_config(hotel.tenant_id)
    )
    assert dangerous.startswith("EMERGENCY")
    assert routine.startswith("NOT_EMERGENCY")


async def test_tools_refuse_to_run_without_a_tenant():
    with pytest.raises(MissingTenantContextError):
        await check_availability.ainvoke({"service": "anything"}, config={"configurable": {}})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(555) 111-2222", "+15551112222"),
        ("555.111.2222", "+15551112222"),
        ("+44 20 7946 0000", "+442079460000"),
        ("15551112222", "+15551112222"),
        ("123", None),
        ("", None),
    ],
)
def test_phone_normalisation(raw, expected):
    assert normalize_phone(raw) == expected


def test_naive_iso_is_read_in_the_tenants_timezone(hotel):
    parsed = parse_iso("2026-07-22T09:00:00", hotel.tz)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.hour == 9


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-01T00:00:00Z",  # model mis-encodes a local date as UTC midnight
        "2026-08-01T00:00:00+05:00",  # or with an outright wrong offset
        "2026-08-01T00:00:00-04:00",  # or the correct local offset
        "2026-08-01T00:00:00",  # or naive
    ],
)
def test_iso_wall_clock_is_always_read_as_local_never_shifting_the_day(hotel, value):
    """Regression: a caller's "Saturday" (Aug 1) must stay Aug 1, whatever
    timezone suffix the model attaches. A UTC 'Z' on a New York hotel used to
    shift midnight back to Friday 8pm — a whole day early — so the bot offered
    and booked the wrong day."""
    parsed = parse_iso(value, hotel.tz)
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 8, 1)
    assert parsed.hour == 0
    assert parsed.tzinfo is hotel.tz
