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

#: The shared "how this chat looks" briefing every chat bot gets, whatever
#: its own prompt says — including a prompt pasted in from another platform,
#: via `_augment` below. This is what makes buttons and carousels a
#: capability of *every* bot rather than of the ones whose operator happened
#: to configure a catalog: the tools are bound unconditionally on chat
#: (`native_tools_for`), and this is where the model is told they exist and
#: when to reach for them.
#:
#: Written as behaviour ("offer buttons whenever…"), not as schema — the
#: tool definitions already carry the schema, and repeating it here would
#: just be two places to keep in sync.
_UI_RULE = """## How this chat looks
You are not writing plain text into a terminal. This chat renders real
buttons, quick replies and image cards, and using them is the difference
between a good bot and a wall of text.

- Whenever you ask something with a few likely answers, call offer_actions
  and give those answers as buttons. Don't ask an open question where three
  buttons would do.
- Whenever you would write a URL, call offer_actions with a button instead.
  Never paste a raw link into your reply, and never say "click the link
  below" — just offer it.
- To offer a menu, or a way back ("Main Menu", "Something else"), use
  buttons whose `reply` is the phrase you want sent back to you.
- When you have several things to show that have pictures — products,
  rooms, locations, events, people — call offer_cards instead of listing
  them. Only fill in fields you actually have; never invent a price, a
  rating or an image URL.
- Offer buttons in the SAME turn as the text they belong to, then stop.
  Don't announce them and don't repeat their labels in your sentence.
- Say NOTHING before calling offer_actions, offer_cards or start_flow.
  They are instant, so there is no wait to fill, and anything you say
  first you will end up saying twice. Write your reply once, either
  before or after — never both."""

#: The one-line addition for a tenant whose `ui.cards` is switched off, so
#: the model isn't invited to call a tool that will refuse.
_NO_CARDS_RULE = "\n- Image cards are turned off for this bot. Describe things in words."


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

    # Empty for a tenant with no link catalog / no flows / cards off — same
    # "contributes nothing but a blank line" convention as ${knowledge_rule}.
    # A tenant with `system_prompt_override` set won't get these unless
    # their override text includes the placeholders, which a prompt pasted
    # in from another platform never does — see `_augment` below, which is
    # what stops that being an invisible dead end.
    links = _render_links(tenant)
    flows = _render_flows(tenant)
    # Chat only: a phone caller can't click anything, and `native_tools_for`
    # doesn't bind either tool on voice, so describing them there would be
    # inviting the model to call something that doesn't exist.
    ui_rule = ""
    if channel == "chat":
        ui_rule = _UI_RULE + ("" if tenant.ui.cards else _NO_CARDS_RULE)

    template = (
        Template(tenant.system_prompt_override)
        if tenant.system_prompt_override
        else _load_template()
    )

    # safe_substitute never raises on a stray '$' or an unknown ${name}, so a
    # non-developer editing content/system-prompt.md (or a tenant's own
    # override, via the admin panel) can't crash a live call.
    rendered = template.safe_substitute(
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
        links=links,
        flows=flows,
        ui_rule=ui_rule,
        # Kept resolving so a prompt written against the pre-9.2 placeholder
        # name doesn't render a literal "${cards_rule}" to the model.
        cards_rule="",
    )
    # `ui_rule` is first: it's the section a pasted prompt most needs and
    # least likely names, and it reads as a preamble to the catalog that
    # follows it.
    return _augment(tenant, rendered, (ui_rule, links, flows))


def _augment(tenant: TenantConfig, rendered: str, sections: tuple[str, ...]) -> str:
    """Append any section the rendered prompt is missing entirely.

    The problem this solves: an operator pastes a prompt written for another
    platform into the admin panel's AI Prompt tab. It's a complete,
    well-written script — and it contains no `${links}` or `${flows}`, so
    the model is never told the button catalog exists and the whole feature
    is silently inert for exactly the bots most likely to want it.

    The check is a substring test against the *rendered* text, not against
    the raw template, so an override that does use `${links}` is left
    completely alone — placement stays the operator's choice and nothing is
    ever duplicated. `prompt_augmentation="placeholder_only"` opts out
    entirely, leaving the admin panel's warning banner as the only signal.
    """
    if tenant.prompt_augmentation != "auto_append":
        return rendered
    missing = [section for section in sections if section and section not in rendered]
    if not missing:
        return rendered
    return rendered.rstrip() + "\n\n" + "\n\n".join(missing) + "\n"


def _render_links(tenant: TenantConfig) -> str:
    """The button catalog, with each entry's *type* spelled out.

    The type matters to the model in a way it didn't in 9.1, when every
    entry was a URL or a handoff: it now has to tell a link (opens a page)
    from a flow shortcut (jumps to a scripted step) to choose sensibly
    between them.
    """
    if not tenant.links:
        return ""

    def _line(link) -> str:
        kind = f"flow → {link.flow}" if link.type == "flow" else link.type
        suffix = f" — {link.description}" if link.description else ""
        return f"  - {link.slug} ({kind}): {link.label}{suffix}"

    return "## Actions you can offer\n" + "\n".join(_line(link) for link in tenant.links)


def _render_flows(tenant: TenantConfig) -> str:
    """The scripted flows `start_flow` can hand off to.

    Shows `description` where the operator wrote one, falling back to the
    node's own first line — a flow with neither would otherwise be a bare id
    the model has no basis to route to.
    """
    if not tenant.flows:
        return ""

    def _line(flow) -> str:
        hint = flow.description or flow.say.strip().splitlines()[0] if flow.say else ""
        return f"  - {flow.id}: {hint}" if hint else f"  - {flow.id}"

    return (
        "## Flows you can start\n"
        + "\n".join(_line(flow) for flow in tenant.flows)
        + "\nCall start_flow the moment the caller wants one of these. Don't "
        "paraphrase the flow's message yourself — it's shown automatically, "
        "and your turn ends there."
    )
