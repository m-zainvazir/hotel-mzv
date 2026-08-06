"""Tool-side tenant context.

LangGraph passes a `RunnableConfig` into every tool call. We put `tenant_id`
and `channel` in `configurable`, so a tool can never accidentally operate
without a tenant scope — the alternative (tenant id as a model-supplied
argument) would let the LLM cross tenants.
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from app.tenancy.loader import tenant_config_from_runnable
from app.tenancy.models import TenantConfig


class MissingTenantContextError(RuntimeError):
    pass


def configurable(config: RunnableConfig | None) -> dict[str, Any]:
    return dict((config or {}).get("configurable") or {})


def tenant_from_config(config: RunnableConfig | None) -> TenantConfig:
    tenant_id = configurable(config).get("tenant_id")
    if not tenant_id:
        raise MissingTenantContextError(
            "tenant_id missing from RunnableConfig.configurable — refusing to run a "
            "tenant-scoped tool without a tenant."
        )
    # Draft-preview aware (Test Agent's "Preview draft" link) — see
    # tenant_config_from_runnable's docstring for why this can never leak
    # into a real caller's turn.
    return tenant_config_from_runnable(str(tenant_id), config)


def channel_from_config(config: RunnableConfig | None) -> str:
    return str(configurable(config).get("channel") or "chat")
