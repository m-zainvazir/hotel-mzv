"""Store factory — in-memory by default, Supabase once configured (Phase 4).

Deliberately not on `InMemoryStore` itself: `app.db.memory_store.get_store`
is `@lru_cache`-d, so a settings-driven branch placed there would pin the
backend choice process-wide on first call, immune to
`app.config.reset_settings_cache()` (see plan Risk 5). This module
re-evaluates the choice on every call — cheap, one attribute read — and only
caches the constructed `SupabaseStore` instance, invalidated by
`reset_store_cache()`.

Application code (tools, channels, scripts) should import `get_store` from
here. Test fixtures import the concrete singleton directly from
`app.db.memory_store` instead (see `tests/conftest.py`), so they reset and
assert against the same object regardless of what a given test's settings
happen to be.
"""

from __future__ import annotations

from app.config import get_settings
from app.db.memory_store import get_store as _get_memory_store
from app.db.supabase_store import SupabaseStore


class SupabaseNotConfiguredError(RuntimeError):
    pass


_supabase_singleton: SupabaseStore | None = None


def get_store():
    """Return the active store: `InMemoryStore` or `SupabaseStore`.

    Missing Supabase config is a hard failure in production — silently
    falling back to in-memory there means bookings vanish on the next
    redeploy with no error at all.
    """
    settings = get_settings()
    if not settings.supabase_url:
        if settings.app_env == "production":
            raise SupabaseNotConfiguredError(
                "APP_ENV=production but SUPABASE_URL is unset — refusing to boot on "
                "an in-memory store that loses every booking on redeploy."
            )
        return _get_memory_store()

    global _supabase_singleton
    if _supabase_singleton is None:
        _supabase_singleton = SupabaseStore()
    return _supabase_singleton


def reset_store_cache() -> None:
    """Test/dev hook — drop the cached `SupabaseStore` so a settings change
    (or a swapped test double) takes effect on the next `get_store()` call."""
    global _supabase_singleton
    _supabase_singleton = None
