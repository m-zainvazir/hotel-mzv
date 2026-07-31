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
    custom = hotel.model_copy(
        update={"system_prompt_override": "You are ${business_name}'s custom receptionist."}
    )
    prompt = render_system_prompt(custom, channel="chat")
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
