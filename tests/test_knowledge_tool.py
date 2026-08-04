"""search_knowledge: conditional binding, empty-result safety, tenant
isolation (Phase 9 Part C).

The "both bind sites agree" guarantee (`app/brain/nodes/reason.py` and
`app/brain/nodes/tools.py`) comes from both calling the *same*
`native_tools_for` function, not from two independently-maintained lists —
so the meaningful proof is a full graph turn that both binds AND executes
the tool successfully, not a static comparison of two lists.
"""

from __future__ import annotations

from app.brain.runner import stream_turn
from app.db.memory_store import get_store
from app.db.models import KnowledgeChunk, KnowledgeDocument
from app.rag.embeddings import EmbeddingError
from app.tools.knowledge_tools import search_knowledge
from app.tools.registry import SLOW_TOOLS, is_slow_tool, native_tools_for
from tests.conftest import ai, tool_config


def _with_knowledge(tenant, **overrides):
    knowledge = tenant.knowledge.model_copy(update={"enabled": True, **overrides})
    return tenant.model_copy(update={"knowledge": knowledge})


async def _fake_embed_text(text: str, *, settings=None) -> list[float]:
    return [1.0, 0.0]


class TestConditionalBinding:
    def test_not_bound_when_knowledge_disabled(self, hotel):
        assert hotel.knowledge.enabled is False
        names = {t.name for t in native_tools_for(hotel, "chat")}
        assert "search_knowledge" not in names

    def test_bound_when_knowledge_enabled(self, hotel):
        tenant = _with_knowledge(hotel)
        names = {t.name for t in native_tools_for(tenant, "chat")}
        assert "search_knowledge" in names

    def test_bound_identically_on_both_channels(self, hotel):
        tenant = _with_knowledge(hotel)
        voice_names = {t.name for t in native_tools_for(tenant, "voice")}
        chat_names = {t.name for t in native_tools_for(tenant, "chat")}
        assert "search_knowledge" in voice_names
        assert "search_knowledge" in chat_names

    def test_the_unconditional_five_are_never_affected(self, hotel):
        tenant = _with_knowledge(hotel)
        names = {t.name for t in native_tools_for(tenant, "chat")}
        assert {
            "check_availability",
            "book_job",
            "send_confirmation",
            "escalate",
            "is_emergency",
        } <= names

    def test_is_a_slow_tool_with_an_acknowledgement_entry(self):
        assert is_slow_tool("search_knowledge") is True
        assert "search_knowledge" in SLOW_TOOLS


class TestDirectToolInvocation:
    async def test_disabled_knowledge_returns_a_plain_string_and_never_embeds(
        self, hotel, monkeypatch
    ):
        called = {"n": 0}

        async def counting_embed(text, *, settings=None):
            called["n"] += 1
            return [1.0]

        monkeypatch.setattr("app.tools.knowledge_tools.embed_text", counting_embed)

        result = await search_knowledge.ainvoke(
            {"query": "anything"}, config=tool_config(hotel.tenant_id)
        )
        assert result == "Nothing on file about that."
        assert called["n"] == 0

    async def test_empty_result_is_a_plain_string_not_an_exception(
        self, hotel, override_tenant, monkeypatch
    ):
        tenant = _with_knowledge(hotel)
        override_tenant(tenant)
        monkeypatch.setattr("app.tools.knowledge_tools.embed_text", _fake_embed_text)

        result = await search_knowledge.ainvoke(
            {"query": "anything at all"}, config=tool_config(tenant.tenant_id)
        )
        assert result == "Nothing on file about that."

    async def test_embedding_failure_returns_a_graceful_string_not_an_exception(
        self, hotel, override_tenant, monkeypatch
    ):
        tenant = _with_knowledge(hotel)
        override_tenant(tenant)

        async def failing_embed(text, *, settings=None):
            raise EmbeddingError("provider unreachable")

        monkeypatch.setattr("app.tools.knowledge_tools.embed_text", failing_embed)

        result = await search_knowledge.ainvoke(
            {"query": "anything"}, config=tool_config(tenant.tenant_id)
        )
        assert "couldn't search" in result.lower()

    async def test_a_real_hit_is_formatted_with_its_source_title(
        self, hotel, override_tenant, monkeypatch
    ):
        tenant = _with_knowledge(hotel)
        override_tenant(tenant)
        monkeypatch.setattr("app.tools.knowledge_tools.embed_text", _fake_embed_text)

        store = get_store()
        doc = await store.aadd_document(
            KnowledgeDocument(tenant_id=tenant.tenant_id, title="Hotel Policies")
        )
        await store.aupsert_chunks(
            tenant.tenant_id,
            [
                KnowledgeChunk(
                    tenant_id=tenant.tenant_id,
                    document_id=doc.id,
                    content="Check-in is at 3pm, check-out is at 11am.",
                    embedding=[1.0, 0.0],
                )
            ],
        )

        result = await search_knowledge.ainvoke(
            {"query": "what time is check-in"}, config=tool_config(tenant.tenant_id)
        )
        assert "Hotel Policies" in result
        assert "Check-in is at 3pm" in result

    async def test_bot_a_never_retrieves_bot_b_chunks_through_the_tool(
        self, hotel, northside, override_tenant, monkeypatch
    ):
        tenant_a = _with_knowledge(hotel)
        tenant_b = _with_knowledge(northside)
        override_tenant(tenant_a)
        override_tenant(tenant_b)
        monkeypatch.setattr("app.tools.knowledge_tools.embed_text", _fake_embed_text)

        store = get_store()
        doc_a = await store.aadd_document(
            KnowledgeDocument(tenant_id=tenant_a.tenant_id, title="A")
        )
        await store.aupsert_chunks(
            tenant_a.tenant_id,
            [
                KnowledgeChunk(
                    tenant_id=tenant_a.tenant_id,
                    document_id=doc_a.id,
                    content="hotel-only secret fact",
                    embedding=[1.0, 0.0],
                )
            ],
        )

        result = await search_knowledge.ainvoke(
            {"query": "secret"}, config=tool_config(tenant_b.tenant_id)
        )
        assert result == "Nothing on file about that."


async def test_search_knowledge_works_end_to_end_through_the_graph(
    scripted, hotel, override_tenant, monkeypatch
):
    """The actual "both bind sites agree" proof: reason() must have bound
    the tool for the model to call it, and the dynamic tools node must have
    resolved the identical tool set to execute it — both true only because
    they call the same native_tools_for, not because two lists were kept in
    sync by hand."""
    tenant = _with_knowledge(hotel)
    override_tenant(tenant)
    monkeypatch.setattr("app.tools.knowledge_tools.embed_text", _fake_embed_text)

    store = get_store()
    doc = await store.aadd_document(KnowledgeDocument(tenant_id=tenant.tenant_id, title="Policies"))
    await store.aupsert_chunks(
        tenant.tenant_id,
        [
            KnowledgeChunk(
                tenant_id=tenant.tenant_id,
                document_id=doc.id,
                content="Check-in is at 3pm.",
                embedding=[1.0, 0.0],
            )
        ],
    )

    scripted(
        ai("Let me check.", [{"name": "search_knowledge", "args": {"query": "check-in time"}}]),
        ai("Check-in is at 3pm."),
    )

    events = [
        event
        async for event in stream_turn(
            text="what time is check-in?", tenant_id=tenant.tenant_id, session_id="knowledge-e2e"
        )
    ]

    assert [e.tool for e in events if e.type == "tool_start"] == ["search_knowledge"]
    result_text = next(e.text for e in events if e.type == "tool_result")
    assert "Check-in is at 3pm." in result_text
    assert "Policies" in result_text

    final = next(e for e in events if e.type == "final")
    assert "3pm" in final.text
