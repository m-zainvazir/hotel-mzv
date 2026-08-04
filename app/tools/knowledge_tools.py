"""Native retrieval tool for a bot's own knowledge base (Phase 9 Part C).

Bound conditionally — only when `tenant.knowledge.enabled`
(`app/tools/registry.py::native_tools_for`) — so a bot with no knowledge base
pays nothing extra: no tool schema in the prompt (the ~1,460-token fixed
floor stays unchanged), no embedding call ever attempted. A model-called
retrieval tool, not always-on prompt injection — the plan's own framing:
the caller asks, the model decides this needs a lookup, only then does an
embedding + vector search happen.
"""

from __future__ import annotations

import logging

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.db.factory import get_store
from app.db.models import KnowledgeHit
from app.rag.embeddings import EmbeddingError, embed_text
from app.tools.context import tenant_from_config

logger = logging.getLogger(__name__)

_NOTHING_FOUND = "Nothing on file about that."


@tool
async def search_knowledge(query: str, config: RunnableConfig = None) -> str:
    """Search this business's own uploaded knowledge base for an answer to
    the caller's question.

    Use this for anything specific to this business that isn't already in
    your instructions — detailed policies, procedures, or product/service
    facts. Do not use it for booking, availability, or anything the other
    tools already handle.
    """
    tenant = tenant_from_config(config)
    if not tenant.knowledge.enabled:
        # Defensive only — native_tools_for shouldn't have bound this tool
        # for a tenant with knowledge disabled in the first place. Never an
        # exception either way: a tool the model was never supposed to be
        # able to call still shouldn't blow up a live turn.
        return _NOTHING_FOUND

    try:
        query_embedding = await embed_text(query)
    except EmbeddingError:
        logger.warning(
            "search_knowledge: embedding failed tenant=%s", tenant.tenant_id, exc_info=True
        )
        return "I couldn't search the knowledge base right now — nothing on file about that."

    if not query_embedding:
        return _NOTHING_FOUND

    store = get_store()
    hits = await store.asearch_chunks(
        tenant.tenant_id,
        query_embedding=query_embedding,
        top_k=tenant.knowledge.top_k,
        min_similarity=tenant.knowledge.min_similarity,
    )

    if not hits:
        return _NOTHING_FOUND

    return _format_hits(hits)


def _format_hits(hits: list[KnowledgeHit]) -> str:
    parts = []
    for hit in hits:
        title = hit.document_title or "Untitled"
        parts.append(f'From "{title}":\n{hit.content}')
    return "\n\n---\n\n".join(parts)
