"""offer_cards + the sanitiser (Phase 9.2).

This is the one place a URL the *model* produced reaches a browser, so most
of these tests are about what gets thrown away. The widget renders on the
client's own website, which is what makes a `javascript:` href here an XSS
on someone else's domain rather than ours.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.brain.runner import stream_turn
from app.flows.cards import sanitize_cards
from app.main import app
from app.tenancy.models import TenantLink, UiSettings
from app.tools.card_tools import Card, CardButton, offer_cards
from app.tools.registry import SLOW_TOOLS, native_tools_for
from tests.conftest import ai, tool_config

_MENU_LINK = TenantLink(slug="main-menu", label="Main Menu", type="reply", value="main menu")


def _cards_tenant(hotel, **ui_settings):
    return hotel.model_copy(update={"ui": UiSettings(**ui_settings), "links": [_MENU_LINK]})


@pytest.fixture
def card_hotel(hotel, override_tenant):
    tenant = _cards_tenant(hotel)
    override_tenant(tenant)
    return tenant


class TestBinding:
    def test_bound_on_chat_with_no_configuration_at_all(self, hotel):
        """The zero-config promise: a bot whose operator wrote only an AI
        prompt still gets the carousel."""
        assert hotel.ui.cards is True
        assert "offer_cards" in {t.name for t in native_tools_for(hotel, "chat")}

    def test_still_bound_when_switched_off_but_refuses_at_call_time(self, hotel, override_tenant):
        """`ui.cards` is a kill switch read inside the tool, not a binding
        gate — so the schema can't vanish mid-conversation."""
        tenant = _cards_tenant(hotel, cards=False)
        override_tenant(tenant)
        assert "offer_cards" in {t.name for t in native_tools_for(tenant, "chat")}

    def test_never_bound_on_voice(self, card_hotel):
        # Reading a carousel aloud is nonsense.
        assert "offer_cards" not in {t.name for t in native_tools_for(card_hotel, "voice")}

    def test_not_a_slow_tool(self):
        assert "offer_cards" not in SLOW_TOOLS


class TestUrlSafety:
    @pytest.mark.parametrize(
        "bad",
        [
            "javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "//evil.example.com/x",
            "not a url at all",
        ],
    )
    def test_a_non_http_url_is_stripped(self, card_hotel, bad):
        cards = sanitize_cards(card_hotel, [Card(title="T", image_url=bad, url=bad)])
        assert cards[0]["image_url"] is None
        assert cards[0]["url"] is None
        # The card itself survives — losing a link shouldn't lose the content.
        assert cards[0]["title"] == "T"

    def test_a_non_http_button_url_drops_that_button_only(self, card_hotel):
        cards = sanitize_cards(
            card_hotel,
            [
                Card(
                    title="T",
                    buttons=[
                        CardButton(label="Evil", url="javascript:alert(1)"),
                        CardButton(label="Fine", url="https://ok.example.com"),
                    ],
                )
            ],
        )
        assert [b["label"] for b in cards[0]["buttons"]] == ["Fine"]

    def test_http_and_https_both_survive(self, card_hotel):
        cards = sanitize_cards(
            card_hotel,
            [Card(title="T", url="http://a.example.com", image_url="https://b.example.com/i.png")],
        )
        assert cards[0]["url"] == "http://a.example.com"
        assert cards[0]["image_url"] == "https://b.example.com/i.png"


class TestHostAllowlist:
    def test_an_off_allowlist_host_is_dropped(self, hotel, override_tenant):
        tenant = _cards_tenant(hotel, allowed_hosts=["amazon.com"])
        override_tenant(tenant)
        cards = sanitize_cards(tenant, [Card(title="T", url="https://evil.example.com/x")])
        assert cards[0]["url"] is None

    def test_an_on_allowlist_host_survives(self, hotel, override_tenant):
        tenant = _cards_tenant(hotel, allowed_hosts=["amazon.com"])
        override_tenant(tenant)
        cards = sanitize_cards(tenant, [Card(title="T", url="https://amazon.com/dp/1")])
        assert cards[0]["url"] == "https://amazon.com/dp/1"

    def test_a_wildcard_entry_covers_subdomains_and_the_bare_domain(self, hotel, override_tenant):
        tenant = _cards_tenant(hotel, allowed_hosts=["*.media-amazon.com"])
        override_tenant(tenant)
        for url in ("https://m.media-amazon.com/i.jpg", "https://media-amazon.com/i.jpg"):
            assert sanitize_cards(tenant, [Card(title="T", url=url)])[0]["url"] == url

    def test_a_lookalike_suffix_does_not_match(self, hotel, override_tenant):
        """`notamazon.com` must not pass an `amazon.com` allowlist — the
        check is on a dot boundary, not a bare `endswith`."""
        tenant = _cards_tenant(hotel, allowed_hosts=["amazon.com"])
        override_tenant(tenant)
        cards = sanitize_cards(tenant, [Card(title="T", url="https://notamazon.com/x")])
        assert cards[0]["url"] is None

    def test_an_empty_allowlist_permits_any_http_host(self, card_hotel):
        assert card_hotel.ui.allowed_hosts == []
        cards = sanitize_cards(card_hotel, [Card(title="T", url="https://anything.example.com")])
        assert cards[0]["url"] == "https://anything.example.com"

    def test_a_catalog_slug_button_bypasses_the_allowlist(self, hotel, override_tenant):
        """The user-facing rule: the allowlist constrains the *model*, never
        the operator. A catalog button on an otherwise-rejected card must
        still render."""
        tenant = _cards_tenant(hotel, allowed_hosts=["amazon.com"])
        override_tenant(tenant)
        cards = sanitize_cards(
            tenant,
            [
                Card(
                    title="T",
                    url="https://evil.example.com",
                    buttons=[
                        CardButton(slug="main-menu"),
                        CardButton(label="Nope", url="https://evil.example.com"),
                    ],
                )
            ],
        )
        assert cards[0]["url"] is None  # the model's own url was dropped
        assert [b["slug"] for b in cards[0]["buttons"]] == ["main-menu"]

    def test_an_unknown_slug_button_is_dropped(self, card_hotel):
        cards = sanitize_cards(
            card_hotel, [Card(title="T", buttons=[CardButton(slug="does-not-exist")])]
        )
        assert cards[0]["buttons"] == []


class TestShaping:
    def test_cards_are_truncated_to_max_cards(self, hotel, override_tenant):
        tenant = _cards_tenant(hotel, max_cards=2)
        override_tenant(tenant)
        cards = sanitize_cards(tenant, [Card(title=f"T{i}") for i in range(9)])
        assert [c["title"] for c in cards] == ["T0", "T1"]

    def test_a_card_with_no_title_is_dropped(self, card_hotel):
        cards = sanitize_cards(card_hotel, [Card(title="  "), Card(title="Real")])
        assert [c["title"] for c in cards] == ["Real"]

    def test_a_slug_button_wins_over_a_model_url_on_the_same_button(self, card_hotel):
        cards = sanitize_cards(
            card_hotel,
            [
                Card(
                    title="T",
                    buttons=[CardButton(slug="main-menu", url="https://evil.example.com")],
                )
            ],
        )
        assert cards[0]["buttons"][0]["slug"] == "main-menu"
        assert cards[0]["buttons"][0]["url"] is None


class TestToolInvocation:
    async def test_returns_a_cards_artifact(self, card_hotel):
        result = await offer_cards.ainvoke(
            {
                "items": [
                    {"title": "Laptop", "subtitle": "$2,599", "url": "https://x.example.com/1"}
                ]
            },
            config=tool_config(card_hotel.tenant_id),
        )
        assert "Laptop" in result

    async def test_nothing_renderable_is_a_plain_no_op(self, card_hotel):
        result = await offer_cards.ainvoke(
            {"items": [{"title": ""}]}, config=tool_config(card_hotel.tenant_id)
        )
        assert result == "No cards could be shown."

    async def test_tenant_b_cannot_resolve_tenant_as_slug(self, card_hotel, northside):
        result = await offer_cards.ainvoke(
            {"items": [{"title": "T", "buttons": [{"slug": "main-menu"}]}]},
            config=tool_config(northside.tenant_id),
        )
        assert "T" in result  # the card renders, but with no buttons


class TestThroughTheGraph:
    async def test_a_cards_event_reaches_the_stream(self, card_hotel, scripted):
        scripted(
            ai(
                "Here's what I found. ",
                [
                    {
                        "name": "offer_cards",
                        "args": {
                            "items": [
                                {
                                    "title": "Thunderobot Zero 16",
                                    "subtitle": "RTX 4080 — $2,599.97",
                                    "image_url": "https://img.example.com/a.jpg",
                                    "url": "https://shop.example.com/a",
                                    "buttons": [{"slug": "main-menu"}],
                                }
                            ]
                        },
                    }
                ],
            ),
            ai("Let me know if you'd like another."),
        )

        events = [
            event
            async for event in stream_turn(
                text="RTX 4000 series laptops",
                tenant_id=card_hotel.tenant_id,
                session_id="cards-stream",
            )
        ]

        cards_events = [e for e in events if e.type == "cards"]
        assert len(cards_events) == 1
        card = cards_events[0].data["cards"][0]
        assert card["title"] == "Thunderobot Zero 16"
        assert card["image_url"] == "https://img.example.com/a.jpg"
        assert [b["label"] for b in card["buttons"]] == ["Main Menu"]

    async def test_a_cards_url_is_not_also_auto_linkified(self, card_hotel, scripted):
        """The auto-linkify fallback must not bolt a duplicate hostname
        button onto a URL that already rendered as a card."""
        url = "https://shop.example.com/a"
        scripted(
            ai(
                f"Found it at {url}. ",
                [{"name": "offer_cards", "args": {"items": [{"title": "A", "url": url}]}}],
            ),
            ai("Anything else?"),
        )

        events = [
            event
            async for event in stream_turn(
                text="find me a laptop",
                tenant_id=card_hotel.tenant_id,
                session_id="cards-no-dupe",
            )
        ]

        assert not any(e.type == "actions" for e in events)
        assert len([e for e in events if e.type == "cards"]) == 1


def test_cards_reach_a_real_chat_sse_stream(card_hotel, scripted):
    scripted(
        ai("", [{"name": "offer_cards", "args": {"items": [{"title": "Room 101"}]}}]),
        ai("Want to book it?"),
    )
    with TestClient(app) as client:
        session = client.post(
            "/chat/session", json={"widget_key": "pk_widget_hotelmzv_demo"}
        ).json()
        response = client.post(
            "/chat",
            json={"message": "show me rooms"},
            headers={"Authorization": f"Bearer {session['token']}"},
        )

    payloads = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    cards = [p for p in payloads if p["type"] == "cards"]
    assert len(cards) == 1
    assert cards[0]["data"]["cards"][0]["title"] == "Room 101"


class TestCardButtonsMatchMessageButtons:
    """A card button and a button under a message are the same idea, so they
    resolve through one function (`resolve_button_spec`). They didn't at
    first: `CardButton` shipped without `reply`, so a card button that sends
    text back was inexpressible and every attempt at one was silently
    dropped — found live, with `buttons: []` on every card of every run."""

    def test_a_reply_button_on_a_card_survives(self, card_hotel):
        cards = sanitize_cards(
            card_hotel,
            [
                Card(
                    title="The Great Gatsby",
                    buttons=[CardButton(label="Ask about this", reply="tell me about Gatsby")],
                )
            ],
        )
        assert cards[0]["buttons"] == [
            {
                "type": "reply",
                "label": "Ask about this",
                "slug": None,
                "url": None,
                "value": "tell me about Gatsby",
                "flow": None,
            }
        ]

    def test_a_bare_label_on_a_card_becomes_a_reply_sending_itself(self, card_hotel):
        cards = sanitize_cards(card_hotel, [Card(title="T", buttons=[CardButton(label="More")])])
        assert cards[0]["buttons"][0]["value"] == "More"

    def test_a_card_button_and_an_action_button_produce_identical_rows(self, card_hotel):
        """The regression guard for the drift itself, not just its symptom."""
        from app.flows.resolver import resolve_button_spec

        spec = CardButton(label="Ask about this", reply="tell me more")
        assert sanitize_cards(card_hotel, [Card(title="T", buttons=[spec])])[0]["buttons"][0] == (
            resolve_button_spec(card_hotel, spec)
        )
