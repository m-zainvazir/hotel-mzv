"""Tenant storage behind a protocol.

Phase 1 reads JSON files. Phase 4 swaps in a Supabase-backed implementation
with RLS; the protocol below is the seam, so no caller changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.tenancy.models import TenantConfig


class TenantNotFoundError(LookupError):
    """Raised when a tenant_id / phone number / widget key resolves to nothing."""


class TenantArchivedError(TenantNotFoundError):
    """Raised when a tenant resolves but is `status: "archived"` (Phase 9
    Part B) — a soft-deleted bot must not answer on any channel.

    Subclasses `TenantNotFoundError` deliberately: every existing
    `except TenantNotFoundError` handler (`app/channels/chat.py`,
    `app/channels/webhooks.py`) already refuses cleanly without any change —
    from a caller's perspective "archived" and "doesn't exist" get the same
    outward refusal, just a different log line at the point it's raised
    (`app/tenancy/loader.py::resolve_tenant_id`).
    """


class ChannelDisabledError(TenantNotFoundError):
    """Raised when a tenant resolves fine but has disabled the channel
    being used (`TenantConfig.channels`, Phase 9.1) — e.g. a chat handshake
    against a voice-only bot. Same subclassing trick as `TenantArchivedError`
    above, for the same reason: every existing `except TenantNotFoundError`
    handler already refuses cleanly with no code change. Raised by
    `app/tenancy/loader.py::require_channel_enabled`, not by
    `resolve_tenant_id` itself — that function has no `channel` argument, so
    it can't know which channel to check.
    """


@runtime_checkable
class TenantRepository(Protocol):
    def get(self, tenant_id: str) -> TenantConfig: ...

    def list_ids(self) -> list[str]: ...

    def find_by_phone(self, phone_number: str) -> TenantConfig | None: ...

    def find_by_widget_key(self, widget_key: str) -> TenantConfig | None: ...

    def find_by_assistant_id(self, assistant_id: str) -> TenantConfig | None: ...


class JsonFileTenantRepository:
    """Reads one `<tenant_id>.json` per tenant from a directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)
        self._parsed: dict[str, TenantConfig] = {}

    def get(self, tenant_id: str) -> TenantConfig:
        if tenant_id in self._parsed:
            return self._parsed[tenant_id]

        path = self._directory / f"{tenant_id}.json"
        if not path.is_file():
            raise TenantNotFoundError(f"no tenant config for {tenant_id!r} in {self._directory}")

        raw = json.loads(path.read_text(encoding="utf-8"))
        config = TenantConfig.model_validate(raw)
        if config.tenant_id != tenant_id:
            raise ValueError(
                f"{path.name} declares tenant_id={config.tenant_id!r}, expected {tenant_id!r}"
            )
        self._parsed[tenant_id] = config
        return config

    def list_ids(self) -> list[str]:
        return sorted(p.stem for p in self._directory.glob("*.json"))

    def find_by_phone(self, phone_number: str) -> TenantConfig | None:
        wanted = _digits(phone_number)
        for tenant_id in self.list_ids():
            config = self.get(tenant_id)
            if any(_digits(n) == wanted for n in config.phone_numbers):
                return config
        return None

    def find_by_widget_key(self, widget_key: str) -> TenantConfig | None:
        for tenant_id in self.list_ids():
            config = self.get(tenant_id)
            if widget_key in config.widget_keys:
                return config
        return None

    def find_by_assistant_id(self, assistant_id: str) -> TenantConfig | None:
        if not assistant_id:
            return None
        for tenant_id in self.list_ids():
            config = self.get(tenant_id)
            if config.vapi.assistant_id == assistant_id:
                return config
        return None

    def invalidate(self) -> None:
        self._parsed.clear()


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())
