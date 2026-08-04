"""The knowledge-base ingestion pipeline (Phase 9 Part C):
extract -> chunk -> embed -> upsert, with per-document status transitions so
the panel's document list reflects real progress instead of a spinner that
could mean anything. Started as a fire-and-forget background task
(`asyncio.create_task`) by the admin routes (`app/channels/admin.py`) — same
pattern chat-transcript writes already use in `app/channels/chat.py` — so a
large upload never holds the HTTP request open.

`start_ingestion_from_*` return as soon as the `KnowledgeDocument` row
exists (`status="pending"`); the actual work happens after the caller has
already gotten its response. `ingest_text` never raises out of the task —
every failure is recorded on the document row instead, since nothing is
awaiting this coroutine's result.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.db.models import KnowledgeChunk, KnowledgeDocument
from app.db.store import KnowledgeStore
from app.rag.chunking import approx_token_count, chunk_text
from app.rag.embeddings import EmbeddingError, embed_texts
from app.rag.extract import ExtractionError, extract_text
from app.tenancy.models import TenantConfig

logger = logging.getLogger(__name__)


async def start_ingestion_from_text(
    store: KnowledgeStore, tenant: TenantConfig, *, title: str, text: str
) -> KnowledgeDocument:
    document = KnowledgeDocument(
        tenant_id=tenant.tenant_id,
        title=title or "Pasted text",
        source_type="text",
        source_ref=title,
        bytes=len(text.encode("utf-8")),
    )
    document = await store.aadd_document(document)
    asyncio.create_task(ingest_text(store, tenant, document, text))
    return document


async def start_ingestion_from_file(
    store: KnowledgeStore, tenant: TenantConfig, *, filename: str, data: bytes
) -> KnowledgeDocument:
    document = KnowledgeDocument(
        tenant_id=tenant.tenant_id,
        title=filename,
        source_type="file",
        source_ref=filename,
        bytes=len(data),
    )
    document = await store.aadd_document(document)
    asyncio.create_task(_ingest_file(store, tenant, document, filename, data))
    return document


async def start_ingestion_from_url(
    store: KnowledgeStore,
    tenant: TenantConfig,
    *,
    url: str,
    crawl: bool,
    max_pages: int = 20,
    max_depth: int = 2,
) -> list[KnowledgeDocument]:
    """One `KnowledgeDocument` per crawled page (or the single fetched page
    when `crawl=False`) — a whole site is many independently-titled,
    independently-retryable documents, not one giant blob."""
    from app.rag.crawl import CrawlError, crawl_site, fetch_page

    if not crawl:
        import httpx

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
                page = await fetch_page(client, url)
        except CrawlError as exc:
            document = await store.aadd_document(
                KnowledgeDocument(
                    tenant_id=tenant.tenant_id,
                    title=url,
                    source_type="url",
                    source_ref=url,
                    status="failed",
                    error=str(exc),
                )
            )
            return [document]
        document = await start_ingestion_from_text(
            store, tenant, title=page.title or page.url, text=page.text
        )
        return [document.model_copy(update={"source_type": "url", "source_ref": page.url})]

    try:
        pages = await crawl_site(url, max_pages=max_pages, max_depth=max_depth)
    except CrawlError as exc:
        document = await store.aadd_document(
            KnowledgeDocument(
                tenant_id=tenant.tenant_id,
                title=url,
                source_type="url",
                source_ref=url,
                status="failed",
                error=str(exc),
            )
        )
        return [document]

    documents: list[KnowledgeDocument] = []
    for page in pages:
        document = KnowledgeDocument(
            tenant_id=tenant.tenant_id,
            title=page.title or page.url,
            source_type="url",
            source_ref=page.url,
            bytes=len(page.text.encode("utf-8")),
        )
        document = await store.aadd_document(document)
        asyncio.create_task(ingest_text(store, tenant, document, page.text))
        documents.append(document)
    return documents


async def _ingest_file(
    store: KnowledgeStore,
    tenant: TenantConfig,
    document: KnowledgeDocument,
    filename: str,
    data: bytes,
) -> None:
    try:
        # Extraction (PDF parsing especially) is CPU-bound and can take real
        # time on a large file — this app is single-worker/single-replica
        # (CLAUDE.md), so blocking the event loop here would stall every
        # other tenant's live conversation turn for the duration.
        text = await asyncio.to_thread(extract_text, filename, data)
    except ExtractionError as exc:
        await store.aset_document_status(
            tenant.tenant_id, document.id, status="failed", error=str(exc)
        )
        return
    await ingest_text(store, tenant, document, text)


async def ingest_text(
    store: KnowledgeStore, tenant: TenantConfig, document: KnowledgeDocument, text: str
) -> None:
    """Chunk, embed, and store `text` as `document`'s content, transitioning
    its status as it goes. Never raises."""
    try:
        await store.aset_document_status(tenant.tenant_id, document.id, status="indexing")

        chunks = chunk_text(text)
        if not chunks:
            await store.aset_document_status(
                tenant.tenant_id, document.id, status="failed", error="no extractable text"
            )
            return

        chunks = await _enforce_quota(store, tenant, document, chunks)
        if chunks is None:
            return

        try:
            vectors = await embed_texts(chunks, settings=get_settings())
        except EmbeddingError as exc:
            logger.warning(
                "knowledge embedding failed tenant=%s document=%s", tenant.tenant_id, document.id
            )
            await store.aset_document_status(
                tenant.tenant_id, document.id, status="failed", error=f"embedding failed: {exc}"
            )
            return

        rows = [
            KnowledgeChunk(
                tenant_id=tenant.tenant_id,
                document_id=document.id,
                ordinal=i,
                content=chunk,
                token_count=approx_token_count(chunk),
                embedding=vector,
            )
            for i, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
        ]
        await store.aupsert_chunks(tenant.tenant_id, rows)
        await store.aset_document_status(
            tenant.tenant_id, document.id, status="ready", chunk_count=len(rows)
        )
    except Exception:
        logger.exception(
            "knowledge ingestion failed unexpectedly tenant=%s document=%s",
            tenant.tenant_id,
            document.id,
        )
        try:
            await store.aset_document_status(
                tenant.tenant_id,
                document.id,
                status="failed",
                error="internal error during ingestion",
            )
        except Exception:
            logger.exception(
                "could not even record the ingestion failure tenant=%s document=%s",
                tenant.tenant_id,
                document.id,
            )


async def _enforce_quota(
    store: KnowledgeStore, tenant: TenantConfig, document: KnowledgeDocument, chunks: list[str]
) -> list[str] | None:
    """`knowledge.max_chunks` (app/tenancy/models.py) is a per-tenant quota
    against the free-tier storage ceiling — Supabase's own limit is shared
    across every tenant, so one bot indexing an unbounded corpus would starve
    every other tenant's quota. Truncates rather than failing outright when
    only *some* of a document's chunks fit; returns `None` when none do (the
    caller has already recorded the failure)."""
    existing = await store.alist_documents(tenant.tenant_id)
    already_indexed = sum(d.chunk_count for d in existing if d.id != document.id)
    budget = tenant.knowledge.max_chunks - already_indexed
    if budget <= 0:
        await store.aset_document_status(
            tenant.tenant_id,
            document.id,
            status="failed",
            error=f"tenant knowledge quota reached ({tenant.knowledge.max_chunks} chunks)",
        )
        return None
    if len(chunks) > budget:
        logger.warning(
            "knowledge quota truncation tenant=%s document=%s wanted=%d budget=%d",
            tenant.tenant_id,
            document.id,
            len(chunks),
            budget,
        )
        return chunks[:budget]
    return chunks
