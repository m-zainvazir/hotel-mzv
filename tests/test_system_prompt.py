"""app/brain/prompts/system.py::render_system_prompt — the shared file
template vs. a tenant's own system_prompt_override (Phase 8 admin panel)."""

from __future__ import annotations

from app.brain.prompts.system import render_system_prompt


def test_default_render_uses_the_shared_template(hotel):
    prompt = render_system_prompt(hotel, channel="chat")
    assert hotel.name in prompt
    assert "## Safety" in prompt
    assert "## Services you can book" in prompt


def test_an_override_replaces_the_shared_template_entirely(hotel):
    """Voice, so the chat-only ${ui_rule} isn't appended — see
    TestPromptAugmentation for the chat behaviour."""
    custom = hotel.model_copy(
        update={"system_prompt_override": "You are ${business_name}'s custom receptionist."}
    )
    prompt = render_system_prompt(custom, channel="voice")
    assert prompt == f"You are {hotel.name}'s custom receptionist."
    assert "## Safety" not in prompt


def test_an_override_still_resolves_placeholders(hotel):
    custom = hotel.model_copy(
        update={"system_prompt_override": "Business: ${business_name}. Hours: ${business_hours}."}
    )
    prompt = render_system_prompt(custom, channel="chat")
    assert hotel.name in prompt
    assert "${business_name}" not in prompt
    assert "${business_hours}" not in prompt


def test_an_empty_string_override_falls_back_to_the_shared_template(hotel):
    custom = hotel.model_copy(update={"system_prompt_override": ""})
    prompt = render_system_prompt(custom, channel="chat")
    assert "## Safety" in prompt


def test_an_override_never_crashes_on_a_stray_dollar_sign_or_unknown_placeholder(hotel):
    custom = hotel.model_copy(
        update={"system_prompt_override": "Price is $5. Unknown: ${not_a_real_placeholder}."}
    )
    prompt = render_system_prompt(custom, channel="chat")
    assert "Price is $5." in prompt


# --- Phase 9.2: buttons, flows, cards ---------------------------------------

_LINKS = [
    {
        "slug": "book",
        "label": "Book online",
        "url": "https://x.example.com/b",
        "description": "the form",
    },
    {"slug": "main-menu", "label": "Main Menu", "type": "flow", "flow": "menu"},
]
_FLOWS = [{"id": "menu", "say": "What can I help with?", "description": "the top-level menu"}]


def _configured(hotel, **update):
    from app.tenancy.models import FlowNode, TenantLink

    base = {
        "links": [TenantLink(**link) for link in _LINKS],
        "flows": [FlowNode(**flow) for flow in _FLOWS],
    }
    return hotel.model_copy(update={**base, **update})


def test_a_tenant_with_no_buttons_or_flows_renders_neither_section(hotel):
    prompt = render_system_prompt(hotel, channel="chat")
    assert "## Actions you can offer" not in prompt
    assert "## Flows you can start" not in prompt


def test_the_catalog_spells_out_each_buttons_type(hotel):
    """New in 9.2: the model has to tell a link (opens a page) from a flow
    shortcut (jumps to a scripted step) to choose between them."""
    prompt = render_system_prompt(_configured(hotel), channel="chat")
    assert "- book (link): Book online — the form" in prompt
    assert "- main-menu (flow → menu): Main Menu" in prompt


def test_flows_are_listed_with_their_description(hotel):
    prompt = render_system_prompt(_configured(hotel), channel="chat")
    assert "## Flows you can start" in prompt
    assert "- menu: the top-level menu" in prompt


def test_a_flow_with_no_description_falls_back_to_its_own_first_line(hotel):
    from app.tenancy.models import FlowNode

    tenant = _configured(hotel, flows=[FlowNode(id="menu", say="What can I help with?")])
    assert "- menu: What can I help with?" in render_system_prompt(tenant, channel="chat")


class TestSharedUiSection:
    """${ui_rule} is what makes buttons a capability of EVERY chat bot
    rather than only the ones whose operator configured a catalog. It is
    the prompt-side half of `native_tools_for` binding the tools
    unconditionally on chat."""

    def test_every_chat_bot_gets_it_with_no_configuration(self, hotel):
        assert hotel.links == []
        assert hotel.flows == []
        prompt = render_system_prompt(hotel, channel="chat")
        assert "## How this chat looks" in prompt
        assert "offer_actions" in prompt
        assert "offer_cards" in prompt

    def test_voice_never_gets_it(self, hotel):
        """Neither tool is bound on voice, so describing them would invite a
        call to something that doesn't exist."""
        prompt = render_system_prompt(hotel, channel="voice")
        assert "## How this chat looks" not in prompt
        assert "offer_cards" not in prompt

    def test_cards_off_tells_the_model_so_instead_of_staying_silent(self, hotel):
        from app.tenancy.models import UiSettings

        off = hotel.model_copy(update={"ui": UiSettings(cards=False)})
        prompt = render_system_prompt(off, channel="chat")
        assert "## How this chat looks" in prompt
        assert "Image cards are turned off" in prompt


class TestPromptAugmentation:
    """A prompt pasted in from another platform has no ${links}/${flows}, so
    without this the model would never learn its buttons exist — which is
    precisely the bot most likely to have been given some."""

    def test_a_pasted_override_gets_the_catalog_appended(self, hotel):
        tenant = _configured(hotel, system_prompt_override="You are a front desk. Be brief.")
        prompt = render_system_prompt(tenant, channel="chat")
        assert prompt.startswith("You are a front desk. Be brief.")
        assert "## Actions you can offer" in prompt
        assert "## Flows you can start" in prompt

    def test_an_override_that_uses_the_placeholder_is_left_alone(self, hotel):
        tenant = _configured(hotel, system_prompt_override="Buttons:\n${links}\n\nThen be brief.")
        prompt = render_system_prompt(tenant, channel="chat")
        # Rendered where the operator put it, and exactly once.
        assert prompt.count("## Actions you can offer") == 1
        assert prompt.index("## Actions you can offer") < prompt.index("Then be brief.")

    def test_placeholder_only_never_appends(self, hotel):
        tenant = _configured(
            hotel,
            system_prompt_override="You are a front desk.",
            prompt_augmentation="placeholder_only",
        )
        prompt = render_system_prompt(tenant, channel="chat")
        assert prompt == "You are a front desk."

    def test_the_shared_template_is_unaffected_since_it_has_the_placeholders(self, hotel):
        prompt = render_system_prompt(_configured(hotel), channel="chat")
        assert prompt.count("## Actions you can offer") == 1
        assert prompt.count("## Flows you can start") == 1

    def test_a_zero_config_override_still_learns_it_has_buttons(self, hotel):
        """The heart of it: an operator pastes a prompt, configures nothing,
        and the bot is still told it can render buttons and cards."""
        tenant = hotel.model_copy(update={"system_prompt_override": "Just this."})
        prompt = render_system_prompt(tenant, channel="chat")
        assert prompt.startswith("Just this.")
        assert "## How this chat looks" in prompt
        # Nothing else to append — no catalog, no flows.
        assert "## Actions you can offer" not in prompt
        assert "## Flows you can start" not in prompt

    def test_an_override_using_ui_rule_is_not_double_appended(self, hotel):
        tenant = hotel.model_copy(update={"system_prompt_override": "Rules:\n${ui_rule}\n\nEnd."})
        prompt = render_system_prompt(tenant, channel="chat")
        assert prompt.count("## How this chat looks") == 1
        assert prompt.index("## How this chat looks") < prompt.index("End.")

    def test_placeholder_only_leaves_a_voice_override_verbatim(self, hotel):
        tenant = hotel.model_copy(
            update={
                "system_prompt_override": "Just this.",
                "prompt_augmentation": "placeholder_only",
            }
        )
        assert render_system_prompt(tenant, channel="chat") == "Just this."
