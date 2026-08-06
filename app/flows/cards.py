"""Sanitising model-supplied card data (Phase 9.2).

This is the one place in the codebase where a URL the *model* produced is
allowed to reach a browser, so it is also the only place that has to assume
the URL is hostile. Two things make that assumption reasonable rather than
paranoid: a card's fields typically originate in a third-party tool result
(a web search, a scraper, a knowledge chunk), and the widget renders on the
*client's own website*, not ours — an XSS here is an XSS on their domain.

Hence the layering:

1. `cards.enabled` is off by default, so a tenant only reaches this code
   after an operator deliberately turned it on.
2. Every URL must parse as `http`/`https`. `javascript:`, `data:`,
   `vbscript:` and friends are rejected outright rather than escaped —
   there is no legitimate card that needs one.
3. `cards.allowed_hosts`, when set, pins which hosts may appear at all.
4. A catalog `slug` button bypasses (3) entirely, because an operator wrote
   it down in tenant config; the allowlist exists to constrain the *model*,
   not the operator.

Nothing here raises. A bad field is dropped, a card with nothing left is
dropped, and an empty result is reported as "no cards" — the same "a partial
answer beats a failed turn" posture `offer_actions` takes for unknown slugs.
Silently degrading is correct at runtime; the loud feedback loop for a
misconfigured allowlist is the admin panel, not a broken conversation.
"""

from __future__ import annotations

import logging
from typing import Any

from app.flows.resolver import resolve_button_spec
from app.flows.urls import safe_url
from app.tenancy.models import TenantConfig

logger = logging.getLogger(__name__)


def sanitize_cards(tenant: TenantConfig, items: list[Any]) -> list[dict[str, Any]]:
    """Turn model-supplied card specs into rows a widget may render.

    `items` are `CardSpec`-shaped (see app/tools/card_tools.py) — read by
    attribute so this stays testable with any simple object and doesn't drag
    the tool's Pydantic models into the sanitiser.
    """
    settings = tenant.ui
    allowed = list(settings.allowed_hosts)

    cards: list[dict[str, Any]] = []
    for item in items[: settings.max_cards]:
        title = (getattr(item, "title", None) or "").strip()
        if not title:
            # No title means nothing identifies the card — the one field
            # with no sensible fallback.
            continue

        # Shared with `offer_actions` — a card button and a button under a
        # message are the same idea, so they resolve through one function.
        buttons = [
            row
            for row in (
                resolve_button_spec(tenant, spec) for spec in getattr(item, "buttons", []) or []
            )
            if row is not None
        ]
        cards.append(
            {
                "title": title,
                "subtitle": (getattr(item, "subtitle", None) or "").strip(),
                "image_url": safe_url(
                    getattr(item, "image_url", None), allowed, what="card image_url"
                ),
                "url": safe_url(getattr(item, "url", None), allowed, what="card url"),
                "buttons": buttons,
            }
        )

    if len(items) > settings.max_cards:
        logger.info(
            "truncated %d cards to max_cards=%d for tenant %s",
            len(items),
            settings.max_cards,
            tenant.tenant_id,
        )
    return cards
