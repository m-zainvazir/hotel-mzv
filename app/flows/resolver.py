"""Slug -> button row, in one place (Phase 9.2).

Phase 9.1 resolved catalog slugs inside `offer_actions` itself, because that
tool was the only thing that rendered a button. 9.2 adds three more render
targets — the greeting menu, a flow node's buttons, and a card's own buttons
— and four independent copies of "look up the slug, drop it if unknown" is
exactly the drift this codebase has been bitten by before (the static
`ToolNode` list vs. `reason`'s per-tenant binding, Phase 6). So there is one
resolver, and everything calls it.

An unknown slug is dropped with a WARNING rather than raising: a partial set
of buttons beats a failed turn, and the same posture already governs
`offer_actions`' unknown slugs and `search_knowledge`'s empty results. The
loud version of this check lives at config-validation time instead
(`TenantConfig._flow_buttons_resolve`), where an operator can actually see
and fix it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.flows.urls import safe_url
from app.tenancy.models import FlowNode, TenantConfig, TenantLink

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedFlow:
    """A flow node with its buttons already turned into wire rows."""

    node: FlowNode
    buttons: list[dict[str, Any]]


def button_row(link: TenantLink) -> dict[str, Any]:
    """One catalog entry as the widget sees it.

    Every key is always present, even when null for this type — a renderer
    that has to check `"url" in item` as well as `item.url` is a renderer
    that will eventually get it wrong.
    """
    return {
        "type": link.type,
        "label": link.label,
        "slug": link.slug,
        "url": link.url,
        "value": link.reply_text() if link.type in ("reply", "handoff") else None,
        "flow": link.flow,
    }


def resolve_button_spec(tenant: TenantConfig, spec: Any) -> dict[str, Any] | None:
    """A model-composed button spec -> one wire row, or None if unusable.

    Shared by `offer_actions` and `offer_cards`, which take structurally
    identical specs (`label` + one of `url` / `reply` / `slug`) and had
    started to drift: `CardButton` shipped without `reply`, so a card button
    that sends text back was inexpressible and every attempt at one was
    silently dropped. Two vocabularies for the same idea is the drift this
    module exists to prevent, so there is one resolver.

    Read by attribute, not by type, so it stays usable from either tool's
    Pydantic model without importing them (and creating a cycle).
    """
    slug = getattr(spec, "slug", None)
    if slug:
        rows = resolve_buttons(tenant, [slug])
        return rows[0] if rows else None

    label = (getattr(spec, "label", None) or "").strip()
    if not label:
        return None

    url = getattr(spec, "url", None)
    if url:
        checked = safe_url(url, list(tenant.ui.allowed_hosts), what="button url")
        if checked is None:
            # Drop the button rather than render a dead one — and rather
            # than fail a whole turn over one bad link.
            return None
        return {
            "type": "link",
            "label": label,
            "slug": None,
            "url": checked,
            "value": None,
            "flow": None,
        }

    # No url and no slug: a quick reply. The label doubles as the text sent
    # back when `reply` is omitted, which is what a chip has always done
    # (widget/src/QuickReplies.tsx).
    return {
        "type": "reply",
        "label": label,
        "slug": None,
        "url": None,
        "value": (getattr(spec, "reply", None) or label).strip(),
        "flow": None,
    }


def resolve_buttons(tenant: TenantConfig, slugs: Sequence[str]) -> list[dict[str, Any]]:
    """Catalog rows for `slugs`, in the order given, unknowns dropped."""
    catalog = {link.slug: link for link in tenant.links}
    rows: list[dict[str, Any]] = []
    for slug in slugs:
        link = catalog.get(slug)
        if link is None:
            logger.warning("unknown button slug %r for tenant %s — dropped", slug, tenant.tenant_id)
            continue
        rows.append(button_row(link))
    return rows


def find_flow(tenant: TenantConfig, flow_id: str) -> FlowNode | None:
    return next((flow for flow in tenant.flows if flow.id == flow_id), None)


def resolve_flow(tenant: TenantConfig, flow_id: str) -> ResolvedFlow | None:
    """The node `flow_id` names, buttons resolved — or None if it's gone.

    None is a real, expected case, not just defensive coding: a visitor can
    be sitting on an open tab whose buttons were rendered before a deploy
    removed that node. Every caller treats None as "fall through to the
    model", so a stale button degrades to an ordinary LLM answer rather than
    a dead turn.
    """
    node = find_flow(tenant, flow_id)
    if node is None:
        return None
    return ResolvedFlow(node=node, buttons=resolve_buttons(tenant, node.buttons))


def resolve_menu(tenant: TenantConfig) -> list[dict[str, Any]]:
    """The persistent menu shown under the greeting, or `[]` for none.

    Sourced from `chat.menu_flow`'s node rather than its own slug list, so
    the `🏠 Main Menu` button and the greeting menu can never drift apart —
    they are literally the same node.
    """
    if not tenant.chat.menu_flow:
        return []
    resolved = resolve_flow(tenant, tenant.chat.menu_flow)
    return resolved.buttons if resolved else []


#: The prefix a widget puts on a `flow` button's postback. Kept here (not in
#: the widget, and not inlined in the runner) so both sides of the wire
#: agree in one place; `parse_postback` is the only thing that reads it.
POSTBACK_FLOW_PREFIX = "flow:"


def parse_postback(postback: str | None) -> str | None:
    """The flow id in a `flow:<id>` postback, or None for anything else.

    Deliberately strict about the prefix rather than treating any string as
    a flow id: postback is a client-supplied field, and a future postback
    kind must not be silently reinterpreted as a flow jump.
    """
    if not postback or not postback.startswith(POSTBACK_FLOW_PREFIX):
        return None
    return postback[len(POSTBACK_FLOW_PREFIX) :] or None
