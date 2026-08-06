"""offer_actions — button rendering, model-authored and catalog alike.

Two halves, and the split is the point. A button may be composed by the
model from its prompt alone (`{label, url}` / `{label, reply}`), which is
what lets an operator configure nothing at all; or it may name a catalog
`slug`, which is how an operator pins one down exactly. Both go through the
same tool and produce the same wire row.

The "both bind sites agree" guarantee (`app/brain/nodes/reason.py` and
`app/brain/nodes/tools.py`) comes from both calling the same
`native_tools_for`, not from two independently-maintained lists — see
test_knowledge_tool.py's identical framing. The meaningful proof is a real
graph turn that both binds AND executes the tool, not a static comparison.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.tenancy.models import FlowNode, TenantConfig, TenantLink
from app.tools.action_tools import offer_actions
from app.tools.registry import SLOW_TOOLS, native_tools_for
from tests.conftest import ai, tool_config

_BOOK_LINK = TenantLink(slug="book-online", label="Book online", url="https://example.com/book")
_CALL_LINK = TenantLink(
    slug="talk-to-someone",
    label="Talk to someone",
    type="handoff",
    description="reach a human",
)


def _with_links(tenant, *links: TenantLink):
    return tenant.model_copy(update={"links": list(links)})


class TestBinding:
    def test_bound_on_chat_with_no_configuration_at_all(self, hotel):
        """The zero-config promise. This used to be gated on `tenant.links`
        being non-empty, which meant a bot could only ever offer buttons an
        operator had already typed out — the opposite of "the only input is
        an AI prompt"."""
        assert hotel.links == []
        assert "offer_actions" in {t.name for t in native_tools_for(hotel, "chat")}

    def test_bound_on_chat_when_links_exist(self, hotel):
        tenant = _with_links(hotel, _BOOK_LINK)
        names = {t.name for t in native_tools_for(tenant, "chat")}
        assert "offer_actions" in names

    def test_never_bound_on_voice_even_with_links(self, hotel):
        """First real use of native_tools_for's `channel` parameter — a
        voice caller can't click a button."""
        tenant = _with_links(hotel, _BOOK_LINK)
        names = {t.name for t in native_tools_for(tenant, "voice")}
        assert "offer_actions" not in names

    def test_the_unconditional_five_are_never_affected(self, hotel):
        tenant = _with_links(hotel, _BOOK_LINK)
        names = {t.name for t in native_tools_for(tenant, "chat")}
        assert {
            "check_availability",
            "book_job",
            "send_confirmation",
            "escalate",
            "is_emergency",
        } <= names

    def test_not_in_slow_tools_its_a_pure_in_memory_lookup(self):
        assert "offer_actions" not in SLOW_TOOLS


class TestDirectToolInvocation:
    async def test_resolves_a_known_slug(self, hotel, override_tenant):
        tenant = _with_links(hotel, _BOOK_LINK)
        override_tenant(tenant)

        result = await offer_actions.ainvoke(
            {"buttons": [{"slug": "book-online"}]}, config=tool_config(tenant.tenant_id)
        )
        assert "Book online" in result

    async def test_unknown_slugs_are_dropped_not_failed(self, hotel, override_tenant):
        tenant = _with_links(hotel, _BOOK_LINK)
        override_tenant(tenant)

        result = await offer_actions.ainvoke(
            {"buttons": [{"slug": "book-online"}, {"slug": "does-not-exist"}]},
            config=tool_config(tenant.tenant_id),
        )
        assert "Book online" in result
        assert "does-not-exist" not in result

    async def test_every_slug_unknown_is_a_plain_no_op(self, hotel, override_tenant):
        tenant = _with_links(hotel, _BOOK_LINK)
        override_tenant(tenant)

        result = await offer_actions.ainvoke(
            {"buttons": [{"slug": "nope"}]}, config=tool_config(tenant.tenant_id)
        )
        assert result == "No matching actions to offer."

    async def test_tenant_a_cannot_resolve_tenant_bs_slugs(self, hotel, northside, override_tenant):
        tenant_a = _with_links(hotel, _BOOK_LINK)
        override_tenant(tenant_a)
        override_tenant(northside)  # northside declares no links of its own

        result = await offer_actions.ainvoke(
            {"buttons": [{"slug": "book-online"}]}, config=tool_config(northside.tenant_id)
        )
        assert result == "No matching actions to offer."


# --- through the graph + /chat ------------------------------------------


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _sse_payloads(body: str) -> list[dict]:
    events = []
    for line in body.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[6:]))
    return events


def _handshake(client, widget_key: str = "pk_widget_hotelmzv_demo") -> dict:
    response = client.post("/chat/session", json={"widget_key": widget_key})
    assert response.status_code == 200
    return response.json()


def test_actions_event_reaches_a_chat_sse_stream(client, scripted, hotel, override_tenant):
    """The real proof: reason() must have bound offer_actions for the model
    to call it, and the dynamic tools node must have executed the identical
    tool set to run it — both true only because they share native_tools_for."""
    tenant = _with_links(hotel, _BOOK_LINK, _CALL_LINK)
    override_tenant(tenant)

    scripted(
        ai(
            "Sure — here are some options. ",
            [
                {
                    "name": "offer_actions",
                    "args": {
                        "buttons": [
                            {"slug": "book-online"},
                            {"slug": "talk-to-someone"},
                            {"slug": "unknown-slug"},
                        ]
                    },
                }
            ],
        ),
        ai("Let me know if you need anything else."),
    )
    session = _handshake(client)

    response = client.post(
        "/chat",
        json={"message": "can I book online or talk to someone?"},
        headers={"Authorization": f"Bearer {session['token']}"},
    )

    assert response.status_code == 200
    payloads = _sse_payloads(response.text)
    actions_events = [p for p in payloads if p["type"] == "actions"]
    assert len(actions_events) == 1
    actions = actions_events[0]["data"]["actions"]
    slugs = {a["slug"] for a in actions}
    assert slugs == {"book-online", "talk-to-someone"}  # the unknown one dropped
    kinds = {a["type"] for a in actions}
    assert kinds == {"link", "handoff"}
    # tool_start/tool_result stay internal, same as every other tool.
    assert not any(p["type"] in ("tool_start", "tool_result") for p in payloads)


# --- auto-linkify: a URL the model just wrote into its reply text ----------
#
# The fallback for when a link was never registered in the catalog at all
# (e.g. an operator typed "redirect them to https://x.example.com" straight
# into the AI Prompt instead of adding a `links` entry) — app/brain/runner.py
# detects it in the final reply text and offers it as a real button, chat
# only, without any tool call at all.


async def test_a_url_the_model_just_says_becomes_a_real_button(client, scripted, hotel):
    scripted(ai("You can find that at https://careers.example.com/openings."))
    session = _handshake(client)

    response = client.post(
        "/chat",
        json={"message": "any job openings?"},
        headers={"Authorization": f"Bearer {session['token']}"},
    )

    assert response.status_code == 200
    payloads = _sse_payloads(response.text)
    actions_events = [p for p in payloads if p["type"] == "actions"]
    assert len(actions_events) == 1
    action = actions_events[0]["data"]["actions"][0]
    assert action["type"] == "link"
    assert action["url"] == "https://careers.example.com/openings"
    assert action["label"] == "careers.example.com"


async def test_voice_never_gets_an_auto_linkified_button(hotel, scripted):
    from app.brain.runner import stream_turn

    scripted(ai("You can find that at https://careers.example.com/openings."))

    events = [
        event
        async for event in stream_turn(
            text="any job openings?",
            tenant_id=hotel.tenant_id,
            session_id="voice-auto-link",
            channel="voice",
        )
    ]
    assert not any(e.type == "actions" for e in events)


async def test_a_url_already_offered_via_the_catalog_is_not_duplicated(
    hotel, override_tenant, scripted
):
    """The model both calls offer_actions for a catalog link AND happens to
    mention its URL in the same reply — the auto-linkify pass must not
    offer a second, redundant button for the identical URL."""
    from app.brain.runner import stream_turn

    tenant = _with_links(hotel, _BOOK_LINK)
    override_tenant(tenant)

    scripted(
        ai(
            f"Book online at {_BOOK_LINK.url}. ",
            [{"name": "offer_actions", "args": {"buttons": [{"slug": "book-online"}]}}],
        ),
        ai("Anything else?"),
    )

    events = [
        event
        async for event in stream_turn(
            text="how do I book?", tenant_id=tenant.tenant_id, session_id="no-dupe"
        )
    ]

    actions_events = [e for e in events if e.type == "actions"]
    assert len(actions_events) == 1  # only the real catalog one, no auto duplicate
    assert actions_events[0].data["actions"][0]["slug"] == "book-online"


async def test_multiple_urls_in_one_reply_each_become_a_button(hotel, scripted):
    from app.brain.runner import stream_turn

    scripted(ai("Try https://a.example.com or https://b.example.com instead."))

    events = [
        event
        async for event in stream_turn(
            text="where can I go?", tenant_id=hotel.tenant_id, session_id="multi-auto-link"
        )
    ]

    actions_events = [e for e in events if e.type == "actions"]
    assert len(actions_events) == 1
    urls = [a["url"] for a in actions_events[0].data["actions"]]
    assert urls == ["https://a.example.com", "https://b.example.com"]


async def test_no_url_in_the_reply_means_no_actions_event(hotel, scripted):
    from app.brain.runner import stream_turn

    scripted(ai("We don't have any information about that, sorry."))

    events = [
        event
        async for event in stream_turn(
            text="tell me something", tenant_id=hotel.tenant_id, session_id="no-url"
        )
    ]

    assert not any(e.type == "actions" for e in events)


# --- Phase 9.2: the catalog grows `reply` and `flow` ------------------------


class TestExtendedButtonTypes:
    def test_a_reply_button_carries_its_own_text(self):
        link = TenantLink(slug="zip", label="Share my zip", type="reply", value="my zip is")
        assert link.reply_text() == "my zip is"

    def test_a_reply_button_without_a_value_falls_back_to_the_label(self):
        assert TenantLink(slug="zip", label="Share my zip", type="reply").reply_text() == (
            "Share my zip"
        )

    def test_a_link_still_requires_a_url(self):
        with pytest.raises(ValidationError):
            TenantLink(slug="x", label="X", type="link")

    def test_a_flow_button_requires_a_flow(self):
        with pytest.raises(ValidationError):
            TenantLink(slug="x", label="X", type="flow")

    def test_a_non_http_url_is_still_refused(self):
        with pytest.raises(ValidationError):
            TenantLink(slug="x", label="X", url="javascript:alert(1)")

    async def test_offer_actions_can_offer_every_type(self, hotel, override_tenant):
        links = [
            _BOOK_LINK,
            _CALL_LINK,
            TenantLink(slug="menu", label="Main Menu", type="flow", flow="m"),
            TenantLink(slug="zip", label="Share zip", type="reply", value="my zip is"),
        ]
        tenant = hotel.model_copy(update={"links": links, "flows": [FlowNode(id="m", say="Menu.")]})
        override_tenant(tenant)

        result = await offer_actions.ainvoke(
            {
                "buttons": [
                    {"slug": "book-online"},
                    {"slug": "talk-to-someone"},
                    {"slug": "menu"},
                    {"slug": "zip"},
                ]
            },
            config=tool_config(tenant.tenant_id),
        )
        for label in ("Book online", "Talk to someone", "Main Menu", "Share zip"):
            assert label in result


class TestCrossReferenceValidation:
    """Dangling references are caught at config-load time, so the admin panel
    422s with a `loc` path instead of the bot rendering a dead button."""

    def _tenant(self, hotel, **update):
        return hotel.model_copy(update=update).model_dump()

    def test_a_button_pointing_at_a_missing_flow_is_refused(self, hotel):
        with pytest.raises(ValidationError, match="does not exist"):
            TenantConfig.model_validate(
                {
                    **hotel.model_dump(),
                    "links": [{"slug": "m", "label": "M", "type": "flow", "flow": "ghost"}],
                }
            )

    def test_a_flow_referencing_a_missing_slug_is_refused(self, hotel):
        with pytest.raises(ValidationError, match="unknown link slugs"):
            TenantConfig.model_validate(
                {**hotel.model_dump(), "flows": [{"id": "f", "say": "s", "buttons": ["ghost"]}]}
            )

    def test_a_menu_flow_that_does_not_exist_is_refused(self, hotel):
        with pytest.raises(ValidationError, match="not a declared flow"):
            TenantConfig.model_validate(
                {**hotel.model_dump(), "chat": {**hotel.chat.model_dump(), "menu_flow": "ghost"}}
            )

    def test_duplicate_flow_ids_are_refused(self, hotel):
        with pytest.raises(ValidationError, match="flow ids must be unique"):
            TenantConfig.model_validate(
                {
                    **hotel.model_dump(),
                    "flows": [{"id": "f", "say": "a"}, {"id": "f", "say": "b"}],
                }
            )

    def test_a_valid_cross_referenced_config_passes(self, hotel):
        config = TenantConfig.model_validate(
            {
                **hotel.model_dump(),
                "links": [{"slug": "m", "label": "M", "type": "flow", "flow": "f"}],
                "flows": [{"id": "f", "say": "s", "buttons": ["m"]}],
                "chat": {**hotel.chat.model_dump(), "menu_flow": "f"},
            }
        )
        assert config.chat.menu_flow == "f"


# --- model-authored buttons: the zero-config path ---------------------------
#
# The whole point of the redesign: an operator writes an AI prompt and
# nothing else, and the bot composes its own buttons from it.


class TestModelAuthoredButtons:
    async def test_a_url_button_needs_no_catalog_entry(self, hotel):
        assert hotel.links == []
        result = await offer_actions.ainvoke(
            {"buttons": [{"label": "Book online", "url": "https://example.com/book"}]},
            config=tool_config(hotel.tenant_id),
        )
        assert "Book online" in result

    async def test_a_reply_button_needs_no_catalog_entry(self, hotel):
        result = await offer_actions.ainvoke(
            {"buttons": [{"label": "📍 Find a Location", "reply": "find a location"}]},
            config=tool_config(hotel.tenant_id),
        )
        assert "Find a Location" in result

    async def test_a_bare_label_becomes_a_quick_reply_sending_itself(self, hotel, scripted):
        from app.brain.runner import stream_turn

        scripted(
            ai("", [{"name": "offer_actions", "args": {"buttons": [{"label": "Yes please"}]}}]),
            ai("Great."),
        )
        events = [
            e
            async for e in stream_turn(
                text="anything else?", tenant_id=hotel.tenant_id, session_id="bare-label"
            )
        ]
        action = [e for e in events if e.type == "actions"][0].data["actions"][0]
        assert action == {
            "type": "reply",
            "label": "Yes please",
            "slug": None,
            "url": None,
            "value": "Yes please",
            "flow": None,
        }

    async def test_a_javascript_url_is_dropped_not_rendered(self, hotel):
        """The protection that replaced the slug indirection. This is the
        only thing between a URL from a poisoned tool result and a real
        <a href> on the client's own website."""
        result = await offer_actions.ainvoke(
            {"buttons": [{"label": "Free stuff", "url": "javascript:alert(1)"}]},
            config=tool_config(hotel.tenant_id),
        )
        assert result == "No matching actions to offer."

    async def test_an_off_allowlist_host_is_dropped(self, hotel, override_tenant):
        from app.tenancy.models import UiSettings

        tenant = hotel.model_copy(update={"ui": UiSettings(allowed_hosts=["example.com"])})
        override_tenant(tenant)
        result = await offer_actions.ainvoke(
            {
                "buttons": [
                    {"label": "Good", "url": "https://example.com/a"},
                    {"label": "Bad", "url": "https://evil.example.net/a"},
                ]
            },
            config=tool_config(tenant.tenant_id),
        )
        assert "Good" in result
        assert "Bad" not in result

    async def test_a_catalog_slug_still_bypasses_the_allowlist(self, hotel, override_tenant):
        from app.tenancy.models import UiSettings

        tenant = hotel.model_copy(
            update={"links": [_BOOK_LINK], "ui": UiSettings(allowed_hosts=["nowhere.example"])}
        )
        override_tenant(tenant)
        result = await offer_actions.ainvoke(
            {"buttons": [{"slug": "book-online"}]}, config=tool_config(tenant.tenant_id)
        )
        # _BOOK_LINK points at example.com, which is NOT on the allowlist —
        # an operator wrote it, so the allowlist doesn't apply to it.
        assert "Book online" in result

    async def test_the_kill_switch_drops_model_buttons_but_keeps_catalog_ones(
        self, hotel, override_tenant
    ):
        from app.tenancy.models import UiSettings

        tenant = hotel.model_copy(update={"links": [_BOOK_LINK], "ui": UiSettings(buttons=False)})
        override_tenant(tenant)
        result = await offer_actions.ainvoke(
            {
                "buttons": [
                    {"slug": "book-online"},
                    {"label": "Invented", "url": "https://example.com/x"},
                ]
            },
            config=tool_config(tenant.tenant_id),
        )
        assert "Book online" in result
        assert "Invented" not in result

    async def test_a_model_button_reaches_a_real_chat_sse_stream(self, client, hotel, scripted):
        scripted(
            ai(
                "Sure — which would you like? ",
                [
                    {
                        "name": "offer_actions",
                        "args": {
                            "buttons": [
                                {"label": "🕐 Opening Hours", "reply": "what are your hours?"},
                                {"label": "🌐 Website", "url": "https://example.com"},
                            ]
                        },
                    }
                ],
            ),
            ai("Let me know."),
        )
        session = _handshake(client)
        response = client.post(
            "/chat",
            json={"message": "hi"},
            headers={"Authorization": f"Bearer {session['token']}"},
        )

        actions = [p for p in _sse_payloads(response.text) if p["type"] == "actions"]
        assert len(actions) == 1
        rendered = actions[0]["data"]["actions"]
        assert [a["type"] for a in rendered] == ["reply", "link"]
        assert rendered[0]["value"] == "what are your hours?"
        assert rendered[1]["url"] == "https://example.com"
