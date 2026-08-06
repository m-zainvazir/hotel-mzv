"""offer_actions — the model's way to say "here's a button".

Chat-only (see `app/tools/registry.py::native_tools_for`) — a voice caller
can't click a button, so binding this there would only invite the model to
read a URL aloud instead.

**This tool changed shape after Phase 9.2.** It used to take a list of
`slugs` referring to a `TenantConfig.links` catalog, which meant a bot could
only offer buttons an operator had typed into config first — no prompt
wording could produce one. The project's actual goal is the opposite: an
operator writes an AI prompt and nothing else, and the bot builds its own
buttons, quick replies and menus from it. So a button may now be composed by
the model:

    {"label": "🌐 Book online", "url": "https://example.com/book"}
    {"label": "📍 Find a Location", "reply": "find a location"}
    {"slug": "main-menu"}                     # still supported, still wins

The slug form is unchanged and remains the way to get a button an operator
controls — including `flow` buttons, which jump to a scripted node with no
model involvement at all and therefore cannot be model-authored by
definition.

Giving up the slug indirection for the other two forms is a real trade-off,
not an oversight: it was what guaranteed a URL from a poisoned knowledge
chunk or a hostile tool result could never become clickable on a client's
own website. What replaces it is `app/flows/urls.py` — scheme validation
always, plus an optional per-tenant `ui.allowed_hosts` — and that is the
only thing standing between a model-supplied URL and a real `<a href>`.
"""

from __future__ import annotations

import logging

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.flows.resolver import resolve_button_spec
from app.tools.context import tenant_from_config

logger = logging.getLogger(__name__)


class ActionButton(BaseModel):
    """One button. Give it a `url`, a `reply`, or a `slug` — never more
    than one kind of destination."""

    label: str | None = Field(
        default=None,
        description="The text shown on the button. Ignored when `slug` is set.",
    )
    url: str | None = Field(
        default=None,
        description="An http(s) address this button opens in a new tab.",
    )
    reply: str | None = Field(
        default=None,
        description=(
            "Text sent back as if the visitor had typed it, so you answer it on the "
            "next turn. This is how you build a menu: one button per choice. Defaults "
            "to the label when omitted."
        ),
    )
    slug: str | None = Field(
        default=None,
        description=(
            "A slug from 'Actions you can offer', if that section exists in your "
            "instructions. Use it when one fits — an operator wrote those deliberately, "
            "and it's the only way to reach a scripted flow."
        ),
    )


@tool(response_format="content_and_artifact")
async def offer_actions(
    buttons: list[ActionButton], config: RunnableConfig = None
) -> tuple[str, dict]:
    """Show the visitor clickable buttons under your reply.

    Use this constantly — it is how this chat presents choices. Offer buttons
    whenever you ask a question with a few likely answers, whenever you'd
    otherwise paste a link, and whenever you list options ("you can book,
    find us, or talk to someone"). Never write a URL into your reply text;
    offer it as a button instead. Never describe a button in prose either —
    just offer it.

    Each button is one of:
      - {"label": "Book online", "url": "https://example.com/book"} — opens a page
      - {"label": "Find a Location", "reply": "find a location"} — sends that
        text back to you, so you handle it on the next turn (this is how you
        build a menu, or a "Main Menu" / "Back" button)
      - {"slug": "..."} — an entry from "Actions you can offer", if you have one

    Offer them in the same turn as the text they belong to. Two to five is
    usually right; more than about six reads as a wall.
    """
    tenant = tenant_from_config(config)

    if not tenant.ui.buttons:
        # Kill switch: catalog buttons still work (an operator authored
        # those), model-composed ones don't.
        buttons = [spec for spec in buttons if spec.slug]

    actions = [
        row for row in (resolve_button_spec(tenant, spec) for spec in buttons) if row is not None
    ]

    if not actions:
        # Empty dict, not omitted — the same "no artifact" convention
        # check_availability's own error branches already established.
        return "No matching actions to offer.", {}

    labels = ", ".join(action["label"] for action in actions)
    return f"Offered: {labels}.", {"kind": "actions", "actions": actions}
