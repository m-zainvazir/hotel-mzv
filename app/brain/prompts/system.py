"""System prompt construction.

The prompt *text* lives in `content/system-prompt.md` so it can be edited
without touching code. This module fills its ${placeholders} with per-tenant
values rendered fresh each turn — which is what lets one graph serve many
trades: a plumber tenant just loads different config (plan §9, feature 2).

A tenant may also set `TenantConfig.system_prompt_override` (Phase 8 admin
panel) to replace the shared file entirely, scoped to that tenant only. The
override still runs through the same `safe_substitute()` call, so an admin
who leaves `${business_name}`-style placeholders in their edited text keeps
those pieces live; removing them just makes that section static.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from string import Template

from app.config import get_settings
from app.tenancy.models import TenantConfig

_VOICE_LENGTH = (
    "one or two spoken sentences. This is a phone call — no lists, no markdown, no emoji. "
    "Say numbers the way a person would ('nine thirty', 'two hundred dollars')."
)
_CHAT_LENGTH = (
    "two or three short sentences. Light markdown is fine; keep lists to three items or fewer."
)


@lru_cache(maxsize=4)
def _template(path_str: str, _mtime: float) -> Template:
    """Load and cache the prompt template.

    `_mtime` is part of the cache key, so editing the file takes effect on the
    next turn without a restart — handy while tuning the wording.
    """
    return Template(Path(path_str).read_text(encoding="utf-8"))


def _load_template() -> Template:
    path = get_settings().content_dir / "system-prompt.md"
    return _template(str(path), path.stat().st_mtime)


def render_system_prompt(
    tenant: TenantConfig,
    *,
    channel: str = "chat",
    now: datetime | None = None,
    emergency: bool = False,
    emergency_reason: str | None = None,
) -> str:
    local_now = (now or datetime.now(tenant.tz)).astimezone(tenant.tz)

    services = "\n".join(
        f"  - {s.name} (slug: {s.slug}, {s.duration_minutes} min"
        + (f", ${s.price_usd:.0f}" if s.price_usd else ", price on quote")
        + (", EMERGENCY" if s.emergency else "")
        + (f") — {s.description}" if s.description else ")")
        for s in tenant.services
    )

    safety = (
        f"Danger signs for a {tenant.trade} include: "
        f"{', '.join(tenant.emergency.keywords[:8])}. "
        "If one comes up, say the safety line, then call escalate immediately — "
        "before anything else, including booking."
    )
    if emergency:
        safety = (
            "!! THIS CALLER HAS ALREADY TRIPPED THE EMERGENCY CLASSIFIER "
            f"({emergency_reason or 'danger keywords detected'}).\n"
            f"Say this now, in your own words: {tenant.emergency.holding_message!r}\n"
            "Then call escalate immediately. Do not book, do not ask routine questions."
        )

    # Empty for a tenant with no knowledge base (Phase 9 Part C) — the
    # placeholder then contributes nothing but a blank line, deliberately
    # not wrapped in its own always-visible section header the way
    # ${safety_rules} is, since that guidance (unlike safety) is often
    # absent entirely.
    knowledge_rule = (
        "You have a knowledge base for this business — call search_knowledge for "
        "anything specific to it that isn't already covered above. Prefer what it "
        "returns over guessing, and say so plainly when it comes back empty rather "
        "than inventing an answer."
        if tenant.knowledge.enabled
        else ""
    )

    template = (
        Template(tenant.system_prompt_override)
        if tenant.system_prompt_override
        else _load_template()
    )

    # safe_substitute never raises on a stray '$' or an unknown ${name}, so a
    # non-developer editing content/system-prompt.md (or a tenant's own
    # override, via the admin panel) can't crash a live call.
    return template.safe_substitute(
        business_name=tenant.name,
        trade=tenant.trade,
        persona=tenant.persona or "friendly, efficient, professional",
        channel=channel,
        local_time=f"{local_now:%A %d %B %Y, %H:%M}",
        timezone=tenant.timezone,
        business_hours=tenant.hours_summary(),
        services=services or "  (none configured)",
        length_rule=_VOICE_LENGTH if channel == "voice" else _CHAT_LENGTH,
        safety_rules=safety,
        knowledge_rule=knowledge_rule,
    )
