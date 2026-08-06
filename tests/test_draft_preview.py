"""Draft-preview conversations (Test Agent's "Preview draft" link, added
after Phase 9.1 shipped) — `stream_turn(..., tenant_config_variant="draft")`
must make a REAL turn actually reason and act on the tenant's current
draft, not just display it, while never letting that leak into any other
conversation for the same tenant, let alone a different one.

This is the security-sensitive half of the feature: everything else in
Phase 9.1 works hard to keep a draft OUT of the runtime read path
(`app/tenancy/loader.py::tenant_config_from_runnable`'s own docstring says
so); this is the one deliberate, narrow exception, and it earns its own
test file for that reason.
"""

from __future__ import annotations

from app.brain.runner import stream_turn
from app.tenancy.models import TenantLink
from tests.conftest import ai


def _draft_of(tenant, **updates):
    return tenant.model_copy(update=updates)


class TestDraftVariantDrivesTheRealTurn:
    async def test_the_system_prompt_reflects_the_draft_not_live(
        self, hotel, scripted, monkeypatch
    ):
        draft = _draft_of(hotel, persona="UNMISTAKABLE_DRAFT_PERSONA_MARKER")

        async def _fake_get_draft(tenant_id, *, client=None):
            return draft, "d1"

        monkeypatch.setattr("app.brain.runner.get_draft", _fake_get_draft)
        model = scripted(ai("hello"))

        events = [
            event
            async for event in stream_turn(
                text="hi",
                tenant_id=hotel.tenant_id,
                session_id="draft-preview-1",
                tenant_config_variant="draft",
            )
        ]

        assert any(e.type == "final" for e in events)
        system_prompt = str(model.seen_prompts[0][0].content)
        assert "UNMISTAKABLE_DRAFT_PERSONA_MARKER" in system_prompt

    async def test_live_variant_never_even_reads_the_draft(self, hotel, scripted, monkeypatch):
        called = {"n": 0}

        async def _spy_get_draft(tenant_id, *, client=None):
            called["n"] += 1
            return _draft_of(hotel, persona="SHOULD_NEVER_APPEAR"), "d1"

        monkeypatch.setattr("app.brain.runner.get_draft", _spy_get_draft)
        model = scripted(ai("hello"))

        async for _ in stream_turn(
            text="hi", tenant_id=hotel.tenant_id, session_id="live-1"
        ):  # tenant_config_variant defaults to "live"
            pass

        assert called["n"] == 0
        assert "SHOULD_NEVER_APPEAR" not in str(model.seen_prompts[0][0].content)

    async def test_no_draft_falls_back_to_live_silently(self, hotel, scripted, monkeypatch):
        async def _no_draft(tenant_id, *, client=None):
            return None, None

        monkeypatch.setattr("app.brain.runner.get_draft", _no_draft)
        model = scripted(ai("hello"))

        events = [
            event
            async for event in stream_turn(
                text="hi",
                tenant_id=hotel.tenant_id,
                session_id="draft-preview-2",
                tenant_config_variant="draft",
            )
        ]

        assert not any(e.type == "error" for e in events)
        assert hotel.persona in str(model.seen_prompts[0][0].content) or hotel.name in str(
            model.seen_prompts[0][0].content
        )

    async def test_the_override_never_leaks_into_a_later_live_turn(
        self, hotel, scripted, monkeypatch
    ):
        """Same tenant, two different sessions, back to back — the draft
        override travels through this ONE call's RunnableConfig only
        (app/brain/runner.py::thread_config), never the shared
        get_tenant_config cache, so a live turn started right after a draft
        preview must come back completely clean."""
        draft = _draft_of(hotel, persona="LEAK_CHECK_DRAFT_MARKER")

        async def _fake_get_draft(tenant_id, *, client=None):
            return draft, "d1"

        monkeypatch.setattr("app.brain.runner.get_draft", _fake_get_draft)
        model = scripted(ai("first"), ai("second"))

        async for _ in stream_turn(
            text="hi",
            tenant_id=hotel.tenant_id,
            session_id="draft-preview-3",
            tenant_config_variant="draft",
        ):
            pass
        async for _ in stream_turn(
            text="hi", tenant_id=hotel.tenant_id, session_id="live-2"
        ):  # a completely separate thread, ordinary live turn
            pass

        assert "LEAK_CHECK_DRAFT_MARKER" in str(model.seen_prompts[0][0].content)
        assert "LEAK_CHECK_DRAFT_MARKER" not in str(model.seen_prompts[1][0].content)

    async def test_a_different_tenants_turn_is_unaffected_by_anothers_draft(
        self, hotel, northside, scripted, monkeypatch
    ):
        hotel_draft = _draft_of(hotel, persona="HOTEL_DRAFT_ONLY")

        async def _fake_get_draft(tenant_id, *, client=None):
            assert tenant_id == hotel.tenant_id  # only ever asked about hotel
            return hotel_draft, "d1"

        monkeypatch.setattr("app.brain.runner.get_draft", _fake_get_draft)
        model = scripted(ai("hotel reply"), ai("northside reply"))

        async for _ in stream_turn(
            text="hi",
            tenant_id=hotel.tenant_id,
            session_id="draft-preview-4",
            tenant_config_variant="draft",
        ):
            pass
        async for _ in stream_turn(
            text="hi", tenant_id=northside.tenant_id, session_id="northside-1"
        ):
            pass

        assert "HOTEL_DRAFT_ONLY" not in str(model.seen_prompts[1][0].content)


class TestDraftVariantReachesToolExecutionToo:
    """The "both bind sites agree" proof (same framing test_action_tools.py
    and test_knowledge_tool.py already use for their own conditional
    tools): reason() binds tools off whatever tenant_config_from_runnable
    returns, and the dynamic tools node executes off the identical call —
    a draft's OWN links catalog, entirely absent from live, has to survive
    both hops intact."""

    async def test_offer_actions_binds_and_resolves_the_drafts_own_links(
        self, hotel, scripted, monkeypatch
    ):
        assert hotel.links == []  # live genuinely has none
        draft = _draft_of(
            hotel,
            links=[
                TenantLink(
                    slug="draft-only-link", label="Draft-only link", url="https://example.com"
                )
            ],
        )

        async def _fake_get_draft(tenant_id, *, client=None):
            return draft, "d1"

        monkeypatch.setattr("app.brain.runner.get_draft", _fake_get_draft)
        scripted(
            ai(
                "here",
                [{"name": "offer_actions", "args": {"buttons": [{"slug": "draft-only-link"}]}}],
            ),
            ai("done"),
        )

        events = [
            event
            async for event in stream_turn(
                text="give me the link",
                tenant_id=hotel.tenant_id,
                session_id="draft-preview-5",
                tenant_config_variant="draft",
            )
        ]

        actions_events = [e for e in events if e.type == "actions"]
        assert len(actions_events) == 1
        assert actions_events[0].data["actions"][0]["slug"] == "draft-only-link"

    async def test_the_drafts_own_slug_does_not_resolve_on_the_live_path(self, hotel, scripted):
        """The other direction, and the one that actually matters.

        This used to assert `offer_actions` wasn't even *bound* for a tenant
        with no live links — true until Phase 9.2 made it unconditional so
        that a zero-config bot could compose its own buttons. Binding is no
        longer evidence of anything; what still must hold is that the
        draft's catalog is invisible to a live turn, so the draft-only slug
        resolves to nothing.
        """
        assert hotel.links == []  # live genuinely has none
        scripted(
            ai(
                "here",
                [
                    {
                        "name": "offer_actions",
                        "args": {"buttons": [{"slug": "draft-only-link"}]},
                    }
                ],
            ),
            ai("done"),
        )

        events = [
            event
            async for event in stream_turn(
                text="give me the link",
                tenant_id=hotel.tenant_id,
                session_id="draft-preview-6",
                # no tenant_config_variant — an ordinary live turn
            )
        ]

        assert not any(e.type == "actions" for e in events)
