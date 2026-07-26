"""Node 1 — resolve the tenant and load its profile (plan §5, step 1)."""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig

from app.brain.state import ReceptionistState
from app.tenancy.loader import get_tenant_config, resolve_tenant_id


async def resolve_tenant(state: ReceptionistState, config: RunnableConfig) -> dict:
    configurable = (config or {}).get("configurable") or {}
    tenant_id = state.get("tenant_id") or configurable.get("tenant_id")

    tenant_id = resolve_tenant_id(
        tenant_id=tenant_id,
        phone_number=configurable.get("called_number"),
        widget_key=configurable.get("widget_key"),
    )
    tenant = get_tenant_config(tenant_id)

    return {
        "tenant_id": tenant_id,
        "channel": state.get("channel") or configurable.get("channel") or "chat",
        "tenant_summary": {
            "name": tenant.name,
            "trade": tenant.trade,
            "timezone": tenant.timezone,
            "greeting": tenant.greeting,
            "services": [s.slug for s in tenant.services],
        },
    }
