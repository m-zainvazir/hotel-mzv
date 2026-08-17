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

import re
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

#: What fills `${business_hours}` when nothing can honestly state the hours —
#: a Cal.com-backed tenant whose calendar didn't answer, or one that has
#: never had a manual grid. The wrong answer here is not "say nothing": the
#: previous code rendered an empty grid as "Mon closed, Tue closed, …", so a
#: bot with unconfigured hours cheerfully told callers it never opens.
_HOURS_UNKNOWN = (
    "not listed here — check with check_availability and quote real times "
    "rather than stating opening hours"
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

#: The line `content/system-prompt.md` renders from `${local_time}`. Matched
#: (not just substituted) because an *override* can contain this text with the
#: date already baked in — see `_with_live_time`.
_TIME_LINE = re.compile(r"^[ \t]*Local time right now:.*$", re.MULTILINE)
#: Same idea for the hours line a saved override may have frozen in — see
#: `_with_live_hours`. Matches our own template's wording, which is what the
#: AI Prompt tab pre-filled and therefore what got frozen.
_HOURS_LINE = re.compile(r"^[ \t]*Business hours:.*$", re.MULTILINE)


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


def raw_template_text() -> str:
    """`content/system-prompt.md` verbatim — placeholders unresolved.

    What the admin panel's AI Prompt tab must pre-fill for a tenant with no
    override. It used to pre-fill `_rendered_system_prompt` instead, which is
    the same text with every `${placeholder}` already substituted, so the
    first save froze them all into literals — including `${local_time}`,
    which is how `hotel-mzv` ended up telling the model it was 4 August for
    the next six days. See `_with_live_time`.
    """
    return _load_template().template


def render_system_prompt(
    tenant: TenantConfig,
    *,
    channel: str = "chat",
    now: datetime | None = None,
    emergency: bool = False,
    emergency_reason: str | None = None,
    business_hours: str | None = None,
) -> str:
    """`business_hours` is Phase 9.4's live value, resolved by the caller
    (`app/tools/booking/schedule.py`) because reading it can mean a network
    call and this function is sync. None means "the caller didn't look" —
    which is the right default for every non-graph caller (tests, the admin
    panel's prompt preview) and falls back to the tenant's own grid."""
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
        business_hours=business_hours or _config_hours(tenant) or _HOURS_UNKNOWN,
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
    augmented = _augment(tenant, rendered, (ui_rule, links, flows))
    # Both unconditional, and deliberately outside `_augment`: a prompt that
    # states the wrong date — or hours the calendar no longer keeps — is a
    # correctness bug, not a missed feature, so
    # `prompt_augmentation="placeholder_only"` must not switch either off.
    # `_with_live_hours` also repairs an override whose `${business_hours}`
    # was frozen into a literal before it ever reached this function.
    return _with_live_hours(_with_live_time(augmented, local_now, tenant.timezone), business_hours)


def _with_live_time(rendered: str, local_now: datetime, timezone: str) -> str:
    """Guarantee the prompt states the *current* date, whatever it said.

    Found live, and it made the bot look stupid in the most ordinary way
    possible: asked for "Saturday", `hotel-mzv` answered "we're fully booked"
    and offered slots on the previous Monday, with Cal.com showing 26 free
    slots that Saturday all along. Its stored `system_prompt_override` began

        Local time right now: Tuesday 04 August 2026, 11:41 (America/New_York)

    six days stale, because the admin panel's AI Prompt tab pre-fills the
    *rendered* prompt (`_rendered_system_prompt`) and saving that froze
    `${local_time}` into a literal. The model then resolved "Saturday" to the
    8th — already past — and `check_availability` clamped the query up to now,
    which is exactly the behaviour its docstring promises.

    Two shapes have to be handled, because two different things go wrong:

    * a frozen copy of our own line — **replaced**, not appended to, since
      leaving both would give the model two contradicting dates;
    * a prompt from another platform that never mentions the date at all
      (`playmouth2` is one) — a model with no date can't resolve "tomorrow"
      any better than one with a wrong date, so a section is appended.

    Fixing the editor to pre-fill the raw template stops *new* prompts
    freezing, but can't help the ones already saved or the next one pasted
    in from somewhere else. This can, on the very next turn, with no edit.
    """
    line = f"Local time right now: {local_now:%A %d %B %Y, %H:%M} ({timezone})"
    if _TIME_LINE.search(rendered):
        # A lambda, not a replacement string: the date carries no backslashes
        # today, but `\g` or `\1` appearing in one would be interpreted.
        return _TIME_LINE.sub(lambda _match: line, rendered)
    return (
        rendered.rstrip()
        + "\n\n## Current date and time\n"
        + line
        + "\nThis line is regenerated every turn. If anything above states a "
        "different date, it is stale — use this one when working out what "
        '"today", "tomorrow" or a named weekday means.\n'
    )


def _with_live_hours(rendered: str, business_hours: str | None) -> str:
    """Guarantee the prompt states the hours the calendar *currently* has.

    The exact sibling of `_with_live_time`, and it exists for the same
    reason — found the same way, on the same tenant. `hotel-mzv`'s stored
    `system_prompt_override` contains

        Business hours: Mon 07:00-22:00, Tue 07:00-22:00, ... Sun 07:00-22:00

    frozen in when the AI Prompt tab pre-filled the *rendered* prompt and
    somebody saved it. There is no `${business_hours}` left in that text, so
    the whole Phase 9.4 chain — Cal.com -> provider -> cache -> prompt —
    resolved perfectly and then delivered its answer to a placeholder that no
    longer existed. Live symptom: opening times were edited in Cal.com, the
    admin panel showed the new ones correctly, and the bot kept reciting the
    old ones even when asked for a day-by-day breakdown.

    Only fires when there IS a live value: with nothing to say, an operator's
    hand-written hours line is better than deleting it. Deliberately outside
    `prompt_augmentation`, like the date — reciting hours the business no
    longer keeps is a correctness bug, not a missed feature.

    Nothing is appended when the line is absent, unlike the date. A prompt
    that never mentions hours is not wrong, just quiet, and `${business_hours}`
    already covers the shared template.
    """
    if not business_hours:
        return rendered
    # A lambda, not a replacement string — hours are user/provider data and a
    # stray `\g` in one would otherwise be interpreted as a group reference.
    return _HOURS_LINE.sub(lambda _match: f"Business hours: {business_hours}", rendered)


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


def _config_hours(tenant: TenantConfig) -> str:
    """The manual grid, but only for a tenant it's actually authoritative for.

    Phase 9.4: once a real calendar is behind the bot, the grid in tenant
    config is whatever someone typed before the calendar took over — nobody
    maintains it, and the admin panel now hides it. Quoting it as a fallback
    would mean confidently reciting hours no one has looked at in months,
    which is worse than declining to state any.
    """
    if tenant.booking.provider != "stub" or not tenant.hours:
        return ""
    return tenant.hours_summary()


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
