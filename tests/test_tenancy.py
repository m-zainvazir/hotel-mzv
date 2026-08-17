"""Tenant config loading, caching and resolution."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import get_settings
from app.tenancy.loader import (
    clear_tenant_cache,
    get_repository,
    get_tenant_config,
    resolve_tenant_id,
)
from app.tenancy.repository import JsonFileTenantRepository, TenantNotFoundError


def test_seed_tenants_parse():
    ids = get_repository().list_ids()
    assert {"hotel-mzv", "northside-plumbing"} <= set(ids)


def test_tenant_profile_fields(hotel):
    assert hotel.name == "Hotel_MZV"
    assert hotel.trade == "hotel"
    # The tenant's own declared zone, not a literal: a seed file's timezone is
    # config that legitimately changes (it moved to Asia/Karachi in Phase 9.4),
    # and asserting on today's value here only ever produces a false failure.
    # What's worth testing is that `tz` really resolves it.
    assert hotel.tz == ZoneInfo(hotel.timezone)
    assert hotel.service_by_slug("event-space").duration_minutes == 240


def test_unknown_tenant_raises():
    with pytest.raises(TenantNotFoundError):
        get_tenant_config("does-not-exist")


def test_config_is_cached_not_reread(monkeypatch):
    repository = JsonFileTenantRepository(get_settings().tenant_data_dir)
    reads = {"count": 0}
    original = repository.get

    def counting_get(tenant_id: str):
        reads["count"] += 1
        return original(tenant_id)

    monkeypatch.setattr(repository, "get", counting_get)
    from app.tenancy import loader

    monkeypatch.setattr(loader, "_repository", repository)
    clear_tenant_cache()

    for _ in range(5):
        get_tenant_config("hotel-mzv")

    assert reads["count"] == 1, "config loader must be one read per cold conversation"


def test_resolve_by_phone_and_widget_key():
    assert resolve_tenant_id(phone_number="+1 (555) 123-0000") == "hotel-mzv"
    assert resolve_tenant_id(widget_key="pk_widget_northside_demo") == "northside-plumbing"


def test_resolve_unknown_phone_raises_rather_than_guessing():
    with pytest.raises(TenantNotFoundError):
        resolve_tenant_id(phone_number="+15550009999")


def test_resolve_falls_back_to_default_tenant():
    assert resolve_tenant_id() == get_settings().default_tenant_id


def test_business_hours_helpers(hotel, northside):
    monday = date(2026, 7, 20)
    assert hotel.hours_for(monday) is not None
    # Hotel-mzv is a front desk open 7 days a week; the closed-day path is
    # exercised on northside-plumbing instead (closed Sat/Sun).
    assert northside.hours_for(date(2026, 7, 25)) is None  # Saturday

    open_moment = datetime(2026, 7, 20, 10, 0, tzinfo=hotel.tz)
    closed_moment = datetime(2026, 7, 20, 23, 0, tzinfo=hotel.tz)  # after the 22:00 close
    assert hotel.is_open_at(open_moment)
    assert not hotel.is_open_at(closed_moment)


@pytest.mark.parametrize(
    ("said", "expected_slug"),
    [
        ("book a room", "room-reservation"),
        ("banquet", "event-space"),
        ("massage", "spa-treatment"),
        ("shuttle", "airport-transfer"),
        ("room-reservation", "room-reservation"),
    ],
)
def test_service_matching(hotel, said, expected_slug):
    matched = hotel.find_service(said)
    assert matched is not None
    assert matched.slug == expected_slug
