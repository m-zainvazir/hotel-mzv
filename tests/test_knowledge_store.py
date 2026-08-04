"""KnowledgeStore: InMemoryStore's pure-Python cosine ranking, and
SupabaseStore's PostgREST/RPC wiring (Phase 9 Part C)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import reset_settings_cache
from app.db.memory_store import InMemoryStore
from app.db.models import KnowledgeChunk, KnowledgeDocument
from app.db.supabase_store import SupabaseStore
from tests.conftest import mock_http


@pytest.fixture(autouse=True)
def _jwt_secret_configured(monkeypatch):
    """SupabaseStore's `_request` mints a tenant JWT on every call
    (`app/db/auth.py::tenant_jwt`), which needs SUPABASE_JWT_SECRET set —
    same requirement `test_admin_write.py` documents for the admin write path."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-jwt-secret-long-enough")
    reset_settings_cache()
    yield
    reset_settings_cache()


class TestInMemoryKnowledgeStore:
    async def test_add_and_get_document(self):
        store = InMemoryStore()
        added = await store.aadd_document(KnowledgeDocument(tenant_id="hotel-mzv", title="FAQ"))
        fetched = await store.aget_document("hotel-mzv", added.id)
        assert fetched == added

    async def test_get_unknown_document_is_none(self):
        store = InMemoryStore()
        assert await store.aget_document("hotel-mzv", "does-not-exist") is None

    async def test_list_documents_scoped_by_tenant(self):
        store = InMemoryStore()
        await store.aadd_document(KnowledgeDocument(tenant_id="hotel-mzv", title="A"))
        await store.aadd_document(KnowledgeDocument(tenant_id="northside-plumbing", title="B"))
        docs = await store.alist_documents("hotel-mzv")
        assert [d.title for d in docs] == ["A"]

    async def test_delete_document_cascades_its_chunks(self):
        store = InMemoryStore()
        doc = await store.aadd_document(KnowledgeDocument(tenant_id="hotel-mzv"))
        await store.aupsert_chunks(
            "hotel-mzv",
            [
                KnowledgeChunk(
                    tenant_id="hotel-mzv", document_id=doc.id, content="x", embedding=[1.0, 0.0]
                )
            ],
        )
        await store.adelete_document("hotel-mzv", doc.id)

        assert await store.aget_document("hotel-mzv", doc.id) is None
        hits = await store.asearch_chunks(
            "hotel-mzv", query_embedding=[1.0, 0.0], top_k=5, min_similarity=0.0
        )
        assert hits == []

    async def test_set_document_status_transitions_and_clears_stale_error(self):
        store = InMemoryStore()
        doc = await store.aadd_document(KnowledgeDocument(tenant_id="hotel-mzv"))

        failed = await store.aset_document_status(
            "hotel-mzv", doc.id, status="failed", error="boom"
        )
        assert failed.status == "failed"
        assert failed.error == "boom"

        retried = await store.aset_document_status("hotel-mzv", doc.id, status="indexing")
        assert retried.status == "indexing"
        assert retried.error is None  # a fresh attempt clears the stale error

    async def test_set_document_status_ready_stamps_indexed_at(self):
        store = InMemoryStore()
        doc = await store.aadd_document(KnowledgeDocument(tenant_id="hotel-mzv"))
        assert doc.indexed_at is None
        ready = await store.aset_document_status("hotel-mzv", doc.id, status="ready", chunk_count=3)
        assert ready.indexed_at is not None
        assert ready.chunk_count == 3

    async def test_set_document_status_unknown_document_raises(self):
        store = InMemoryStore()
        with pytest.raises(KeyError):
            await store.aset_document_status("hotel-mzv", "does-not-exist", status="ready")

    async def test_zero_row_search_is_empty_list_not_an_error(self):
        store = InMemoryStore()
        hits = await store.asearch_chunks(
            "hotel-mzv", query_embedding=[1.0, 0.0], top_k=5, min_similarity=0.0
        )
        assert hits == []

    async def test_search_ranks_by_cosine_similarity(self):
        store = InMemoryStore()
        doc = await store.aadd_document(KnowledgeDocument(tenant_id="hotel-mzv", title="Doc"))
        await store.aupsert_chunks(
            "hotel-mzv",
            [
                KnowledgeChunk(
                    tenant_id="hotel-mzv",
                    document_id=doc.id,
                    ordinal=0,
                    content="close match",
                    embedding=[1.0, 0.0, 0.0],
                ),
                KnowledgeChunk(
                    tenant_id="hotel-mzv",
                    document_id=doc.id,
                    ordinal=1,
                    content="far match",
                    embedding=[0.0, 1.0, 0.0],
                ),
            ],
        )
        hits = await store.asearch_chunks(
            "hotel-mzv", query_embedding=[1.0, 0.0, 0.0], top_k=5, min_similarity=0.0
        )
        assert hits[0].content == "close match"
        assert hits[0].similarity > hits[1].similarity
        assert hits[0].document_title == "Doc"

    async def test_min_similarity_cutoff_excludes_weak_matches(self):
        store = InMemoryStore()
        doc = await store.aadd_document(KnowledgeDocument(tenant_id="hotel-mzv"))
        await store.aupsert_chunks(
            "hotel-mzv",
            [
                KnowledgeChunk(
                    tenant_id="hotel-mzv",
                    document_id=doc.id,
                    content="orthogonal",
                    embedding=[0.0, 1.0],
                )
            ],
        )
        hits = await store.asearch_chunks(
            "hotel-mzv", query_embedding=[1.0, 0.0], top_k=5, min_similarity=0.5
        )
        assert hits == []

    async def test_top_k_limits_results(self):
        store = InMemoryStore()
        doc = await store.aadd_document(KnowledgeDocument(tenant_id="hotel-mzv"))
        chunks = [
            KnowledgeChunk(
                tenant_id="hotel-mzv",
                document_id=doc.id,
                ordinal=i,
                content=f"c{i}",
                embedding=[1.0, 0.0],
            )
            for i in range(5)
        ]
        await store.aupsert_chunks("hotel-mzv", chunks)
        hits = await store.asearch_chunks(
            "hotel-mzv", query_embedding=[1.0, 0.0], top_k=2, min_similarity=0.0
        )
        assert len(hits) == 2

    async def test_chunks_without_embeddings_are_skipped(self):
        store = InMemoryStore()
        doc = await store.aadd_document(KnowledgeDocument(tenant_id="hotel-mzv"))
        await store.aupsert_chunks(
            "hotel-mzv",
            [
                KnowledgeChunk(
                    tenant_id="hotel-mzv",
                    document_id=doc.id,
                    content="not embedded yet",
                    embedding=None,
                )
            ],
        )
        hits = await store.asearch_chunks(
            "hotel-mzv", query_embedding=[1.0, 0.0], top_k=5, min_similarity=0.0
        )
        assert hits == []

    async def test_bot_a_never_retrieves_bot_b_chunks(self):
        store = InMemoryStore()
        doc_a = await store.aadd_document(KnowledgeDocument(tenant_id="hotel-mzv"))
        await store.aupsert_chunks(
            "hotel-mzv",
            [
                KnowledgeChunk(
                    tenant_id="hotel-mzv",
                    document_id=doc_a.id,
                    content="hotel secret",
                    embedding=[1.0, 0.0],
                )
            ],
        )
        doc_b = await store.aadd_document(KnowledgeDocument(tenant_id="northside-plumbing"))
        await store.aupsert_chunks(
            "northside-plumbing",
            [
                KnowledgeChunk(
                    tenant_id="northside-plumbing",
                    document_id=doc_b.id,
                    content="plumbing secret",
                    embedding=[1.0, 0.0],
                )
            ],
        )

        hits_a = await store.asearch_chunks(
            "hotel-mzv", query_embedding=[1.0, 0.0], top_k=10, min_similarity=0.0
        )
        hits_b = await store.asearch_chunks(
            "northside-plumbing", query_embedding=[1.0, 0.0], top_k=10, min_similarity=0.0
        )
        assert {h.content for h in hits_a} == {"hotel secret"}
        assert {h.content for h in hits_b} == {"plumbing secret"}

    async def test_reset_clears_knowledge_state(self):
        store = InMemoryStore()
        await store.aadd_document(KnowledgeDocument(tenant_id="hotel-mzv"))
        store.reset()
        assert await store.alist_documents("hotel-mzv") == []


class TestSupabaseKnowledgeStore:
    async def test_search_chunks_posts_to_the_rpc_with_tenant_jwt_not_secret_key(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/rpc/match_knowledge_chunks"
            auth = request.headers["authorization"]
            assert auth.startswith("Bearer ")
            # A tenant JWT, not the raw secret key literal.
            assert auth != "Bearer test-secret-key"
            return httpx.Response(
                200,
                json=[
                    {
                        "chunk_id": "c1",
                        "document_id": "d1",
                        "document_title": "Policies",
                        "content": "check-in is 3pm",
                        "similarity": 0.91,
                    }
                ],
            )

        client, requests = mock_http(handler)
        store = SupabaseStore(client=client)

        hits = await store.asearch_chunks(
            "hotel-mzv", query_embedding=[0.1, 0.2], top_k=4, min_similarity=0.35
        )

        assert len(hits) == 1
        assert hits[0].chunk_id == "c1"
        assert hits[0].document_title == "Policies"
        assert hits[0].similarity == 0.91

        body = json.loads(requests[0].content)
        assert body["query_embedding"] == [0.1, 0.2]
        assert body["match_count"] == 4
        assert body["min_similarity"] == 0.35

    async def test_zero_row_search_is_empty_list_not_an_error(self):
        client, _requests = mock_http(lambda req: httpx.Response(200, json=[]))
        store = SupabaseStore(client=client)
        hits = await store.asearch_chunks(
            "hotel-mzv", query_embedding=[0.1], top_k=4, min_similarity=0.35
        )
        assert hits == []

    async def test_list_documents_carries_an_explicit_tenant_id_filter(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        client, _requests = mock_http(handler)
        store = SupabaseStore(client=client)
        await store.alist_documents("hotel-mzv")

        assert seen["params"]["tenant_id"] == "eq.hotel-mzv"

    async def test_delete_document_carries_an_explicit_tenant_id_filter(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        client, _requests = mock_http(handler)
        store = SupabaseStore(client=client)
        await store.adelete_document("hotel-mzv", "doc-1")

        assert seen["params"]["tenant_id"] == "eq.hotel-mzv"
        assert seen["params"]["id"] == "eq.doc-1"

    async def test_upsert_chunks_skips_the_request_when_empty(self):
        client, requests = mock_http(lambda req: httpx.Response(200, json=[]))
        store = SupabaseStore(client=client)
        await store.aupsert_chunks("hotel-mzv", [])
        assert requests == []
