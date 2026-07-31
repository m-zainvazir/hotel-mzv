"""SupabaseTenantRepository — the Phase 8 read-path flip.

Everything here stays offline via `mock_http` + constructor injection, the
same pattern `CalcomBookingProvider`/`TwilioNotifier`/`SupabaseStore` already
use. `hermetic_settings` + `isolated_runtime` keep `tenant_source` at its
"json" default regardless, so none of this touches the real network guard
differently than any other test.
"""

from __future__ import annotations

import httpx
import pytest

from app.tenancy.repository import JsonFileTenantRepository
from app.tenancy.supabase_repository import SupabaseTenantRepository
from app.tenancy.sync import _service_rows, _tenant_row
from tests.conftest import mock_http


def _row_for(tenant, *, services=True) -> dict:
    row = _tenant_row(tenant)
    if services:
        row["services"] = _service_rows(tenant)
    return row


def _client_returning(rows: list[dict]) -> tuple[httpx.AsyncClient, list]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    return mock_http(handler)


async def test_round_trip_fidelity_reproduces_the_committed_config(hotel):
    """The single most valuable test in the phase: this is what silently
    breaks the moment anyone edits `_TENANT_COLUMNS` without updating the
    hydration side too."""
    client, _ = _client_returning([_row_for(hotel)])
    repo = SupabaseTenantRepository(
        fallback=JsonFileTenantRepository(directory=_content_dir()),
        client=client,
    )
    await repo.refresh()
    assert repo.get("hotel-mzv") == hotel


async def test_malformed_row_falls_back_to_json_others_still_load(hotel, northside):
    good_row = _row_for(hotel)
    bad_row = _tenant_row(northside)
    bad_row["config"]["emergency"] = {}  # escalation_phone is required — invalid
    bad_row["services"] = []

    client, _ = _client_returning([good_row, bad_row])
    fallback = JsonFileTenantRepository(directory=northside.tenant_id and _content_dir())
    repo = SupabaseTenantRepository(fallback=fallback, client=client)
    await repo.refresh()

    assert repo.get("hotel-mzv") == hotel
    assert repo.get("northside-plumbing") == northside  # served from the JSON fallback
    assert repo.degraded is True


async def test_wholesale_failure_serves_fallback_and_sets_degraded(hotel):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client, _ = mock_http(handler)
    fallback = JsonFileTenantRepository(directory=_content_dir())
    repo = SupabaseTenantRepository(fallback=fallback, client=client)
    await repo.refresh()

    assert repo.degraded is True
    assert repo.get("hotel-mzv") == hotel
    assert repo.list_ids() == fallback.list_ids()


async def test_no_supabase_config_degrades_without_raising(hotel):
    """`hermetic_settings` already strips SUPABASE_URL/SUPABASE_SECRET_KEY from
    the test environment, so `refresh()` hits its own "no config" branch with
    no client at all — never a real socket."""
    fallback = JsonFileTenantRepository(directory=_content_dir())
    repo = SupabaseTenantRepository(fallback=fallback)
    await repo.refresh()

    assert repo.degraded is True
    assert repo.get("hotel-mzv") == hotel


@pytest.mark.parametrize(
    "raw",
    ["+1 (555) 123-0000", "15551230000", "+15551230000"],
)
async def test_find_by_phone_normalises_digits_like_the_json_repository(raw, hotel):
    client, _ = _client_returning([_row_for(hotel)])
    supabase_repo = SupabaseTenantRepository(
        fallback=JsonFileTenantRepository(directory=_content_dir()), client=client
    )
    await supabase_repo.refresh()
    json_repo = JsonFileTenantRepository(directory=_content_dir())

    found_supabase = supabase_repo.find_by_phone(raw)
    found_json = json_repo.find_by_phone(raw)

    assert found_supabase is not None
    assert found_json is not None
    assert found_supabase.tenant_id == found_json.tenant_id == "hotel-mzv"


async def test_find_by_assistant_id_reads_through_the_config_blob(hotel):
    client, _ = _client_returning([_row_for(hotel)])
    repo = SupabaseTenantRepository(
        fallback=JsonFileTenantRepository(directory=_content_dir()), client=client
    )
    await repo.refresh()

    found = repo.find_by_assistant_id(hotel.vapi.assistant_id)
    assert found is not None
    assert found.tenant_id == "hotel-mzv"
    assert repo.find_by_assistant_id("no-such-assistant") is None
    assert repo.find_by_assistant_id("") is None


async def test_refresh_picks_up_a_changed_row(hotel):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        row = _row_for(hotel)
        if calls["n"] > 1:
            row["config"]["greeting"] = "Updated greeting from Supabase"
        return httpx.Response(200, json=[row])

    client, _ = mock_http(handler)
    repo = SupabaseTenantRepository(
        fallback=JsonFileTenantRepository(directory=_content_dir()), client=client
    )
    await repo.refresh()
    assert repo.get("hotel-mzv").greeting == hotel.greeting

    await repo.refresh()
    assert repo.get("hotel-mzv").greeting == "Updated greeting from Supabase"


def _content_dir():
    from app.config import get_settings

    return get_settings().tenant_data_dir
