"""offer_cards — the generic-template carousel (Phase 9.2).

An image, a title, a subtitle and its own buttons, repeated horizontally.
The canonical use is a bot that searched or scraped something at runtime (a
product list, a set of locations, an events calendar) and wants to show it
rather than describe it — but it's equally the right answer for anything a
prompt describes as a list of things with pictures.

Bound on every chat tenant and on by default (`ui.cards`), because the
project's goal is that an operator writes an AI prompt and configures
nothing. Card data is model-supplied by necessity: a scraped product's image
and link are discovered mid-turn and cannot come from a catalog. Phase 9.1's
"the model never emits a URL" rule is therefore replaced rather than
abandoned — `ui.allowed_hosts` pins which hosts may appear, and every URL is
scheme-checked before it reaches a browser (`app/flows/urls.py`, via
`app/flows/cards.py::sanitize_cards`). A card's *buttons* can still be plain
catalog slugs, which skip the allowlist entirely because an operator wrote
them down.

Not in `SLOW_TOOLS`: this is an in-memory transform, same as `offer_actions`.
Whatever produced the data (an MCP search, usually) is already slow-by-
default via `is_slow_tool`'s "anything not in the fixed five" fallback, so
the spoken acknowledgement fires where it belongs — before the search, not
before the formatting.
"""

from __future__ import annotations

import logging

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.flows.cards import sanitize_cards
from app.tools.context import tenant_from_config

logger = logging.getLogger(__name__)


class CardButton(BaseModel):
    """One button under a card — the same three shapes `offer_actions`
    takes, deliberately.

    `reply` was missing until a live test asked for a card button that sends
    text back ("Ask about this book") and got silently dropped: with only
    `url` and `slug` available there was no way to express it, so
    `_clean_button` correctly rejected every one. A card button and a reply
    button underneath a message are the same idea in two places; they should
    not have different vocabularies.

    `slug` wins over the others when given: an operator-authored catalog
    entry is always preferable to something the model assembled.
    """

    label: str | None = Field(
        default=None, description="Button text. Ignored when `slug` is set — the catalog wins."
    )
    url: str | None = Field(
        default=None, description="An http(s) URL this button opens. Use with `label`."
    )
    reply: str | None = Field(
        default=None,
        description=(
            "Text sent back as if the visitor typed it, so you answer it on the next "
            "turn — e.g. 'tell me more about The Great Gatsby'. Use with `label`."
        ),
    )
    slug: str | None = Field(
        default=None,
        description=(
            "A slug from 'Actions you can offer'. Prefer this over url whenever "
            "one fits — it's how you offer a flow shortcut or a human handoff "
            "from a card."
        ),
    )


class Card(BaseModel):
    """One item in the carousel."""

    title: str = Field(description="The card's heading. Required — a card without one is dropped.")
    subtitle: str = Field(default="", description="One or two lines under the title.")
    image_url: str | None = Field(default=None, description="An http(s) image URL.")
    url: str | None = Field(default=None, description="Where tapping the card itself goes.")
    buttons: list[CardButton] = Field(default_factory=list)


@tool(response_format="content_and_artifact")
async def offer_cards(items: list[Card], config: RunnableConfig = None) -> tuple[str, dict]:
    """Show the caller a swipeable carousel of cards — each with a picture,
    a title, a short description and its own buttons.

    Use this whenever you have several things to show that have images or
    links (products, locations, events, rooms) instead of listing them as
    text. Only include fields you actually have: leave a field out rather
    than inventing a price, a rating or an image.
    """
    tenant = tenant_from_config(config)
    if not tenant.ui.cards:
        logger.info("offer_cards called but ui.cards is off for %s", tenant.tenant_id)
        return "Cards are not available for this bot — answer in text instead.", {}

    cards = sanitize_cards(tenant, list(items))

    if not cards:
        # Same convention as offer_actions' "nothing resolved" branch: an
        # empty artifact, and text the model can act on rather than a raise.
        return "No cards could be shown.", {}

    titles = ", ".join(card["title"] for card in cards)
    return f"Showed {len(cards)} card(s): {titles}.", {"kind": "cards", "cards": cards}
