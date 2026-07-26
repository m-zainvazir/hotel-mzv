"""Tenant isolation (CLAUDE.md convention #3).

Phase 1 has one process and one in-memory store, so these guarantees live in
the application layer. Phase 4 adds Supabase RLS *underneath* them — belt and
braces, not a replacement. These tests must keep passing after that swap.
"""

from __future__ import annotations

from app.brain.runner import stream_turn
from app.db.memory_store import get_store
from app.tools.booking_tools import book_job, check_availability
from app.tools.messaging_tools import escalate, send_confirmation
from tests.conftest import ai, tool_config


async def _book_for(tenant, service="room-reservation") -> str:
    listing = await check_availability.ainvoke(
        {"service": service}, config=tool_config(tenant.tenant_id)
    )
    slot = listing.split("slot_start_iso=")[1].split(")")[0]
    booked = await book_job.ainvoke(
        {
            "service": service,
            "slot_start_iso": slot,
            "customer_name": "Dana Reyes",
            "customer_phone": "5551112222",
            "address": "12 Oak Street",
        },
        config=tool_config(tenant.tenant_id),
    )
    assert booked.startswith("BOOKED"), booked
    return booked.split("job_id=")[1].split()[0]


async def test_jobs_are_visible_only_to_their_own_tenant(hotel, northside):
    await _book_for(hotel)

    assert len(get_store().list_jobs(hotel.tenant_id)) == 1
    assert get_store().list_jobs(northside.tenant_id) == []


async def test_one_tenant_cannot_confirm_anothers_job(hotel, northside):
    job_id = await _book_for(hotel)

    leaked = await send_confirmation.ainvoke(
        {"job_id": job_id}, config=tool_config(northside.tenant_id)
    )

    assert leaked.startswith("ERROR:")
    assert get_store().list_messages(northside.tenant_id) == []


async def test_escalation_alerts_go_to_the_right_on_call_number(hotel, northside):
    await escalate.ainvoke({"reason": "gas smell"}, config=tool_config(northside.tenant_id))

    northside_alerts = get_store().list_messages(northside.tenant_id)
    assert len(northside_alerts) == 1
    assert northside_alerts[0].to == northside.emergency.escalation_phone
    assert northside_alerts[0].to != hotel.emergency.escalation_phone
    assert get_store().list_messages(hotel.tenant_id) == []


async def test_a_tenants_calendar_is_not_blocked_by_another_tenants_bookings(hotel, northside):
    """Two businesses booking the same wall-clock slot must not collide."""
    await _book_for(hotel)
    booked = await _book_for(northside, service="drain-clearing")

    assert booked
    assert len(get_store().list_jobs(hotel.tenant_id)) == 1
    assert len(get_store().list_jobs(northside.tenant_id)) == 1


async def test_conversations_never_share_a_thread_across_tenants(scripted, hotel, northside):
    """Same session id, different tenants — the histories must stay apart."""
    model = scripted(
        ai("Hotel_MZV here, how can I help?"),
        ai("Northside here, what's up?"),
    )

    async for _ in stream_turn(
        text="my panel is buzzing", tenant_id=hotel.tenant_id, session_id="shared"
    ):
        pass
    async for _ in stream_turn(text="hello", tenant_id=northside.tenant_id, session_id="shared"):
        pass

    northside_prompt = " ".join(str(m.content) for m in model.seen_prompts[-1])
    assert "my panel is buzzing" not in northside_prompt
    assert "Hotel_MZV" not in northside_prompt
    assert "Northside Plumbing" in northside_prompt


async def test_tools_see_only_the_tenant_in_their_runnable_config(hotel, northside):
    """A tool must derive its tenant from config, never from model arguments."""
    listing = await check_availability.ainvoke(
        {"service": "drain clearing"}, config=tool_config(hotel.tenant_id)
    )

    # "drain clearing" is a Northside service; Hotel_MZV must not be able to book it.
    assert "Drain clearing" not in listing
