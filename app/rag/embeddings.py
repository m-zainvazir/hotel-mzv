"""Text embeddings for the knowledge base (Phase 9 Part C).

Raw httpx against Gemini's REST API — no SDK, matching this project's
established precedent for everything that isn't the reasoning model itself
(`app/tools/booking/calcom.py`, `app/mcp/oauth.py`, `scripts/check_model.py`'s
own hardcoded `GOOGLE_BASE`). Dispatches on `embedding_provider` so an
OpenAI implementation later is a config flip, not a refactor — only
`"google"` exists today.

**Unverified against a live account** (same caveat class as
`app/tools/booking/mcp_calcom.py`'s Cal.com MCP tool shapes): Gemini's
`batchEmbedContents` endpoint and per-request `outputDimensionality`
truncation are implemented per the documented request/response shape, but
this repo has not yet made a real call. Confirm live before trusting it in
production — a schema mismatch here would surface as an `EmbeddingError` on
every ingestion, not a silent wrong answer, so the failure mode is at least
loud.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.tools.http_client import shared_async_client

logger = logging.getLogger(__name__)

#: Gemini's own batchEmbedContents cap. Also just a sane per-request bound —
#: a single ingestion can easily produce more chunks than this.
_MAX_BATCH = 100


class EmbeddingError(RuntimeError):
    """The embedding provider could not be reached, rejected the request, or
    returned a shape this module doesn't understand. Callers
    (`app/rag/ingest.py`) map this to a document's `status="failed"` — never
    let it propagate as a bare exception into a background task."""


async def embed_texts(texts: list[str], *, settings: Settings | None = None) -> list[list[float]]:
    """Embed `texts` in order, batched against the provider's own batch
    endpoint — one HTTP round trip per `_MAX_BATCH` texts, not one per text.
    Returns `[]` for empty input without a network call.
    """
    if not texts:
        return []
    settings = settings or get_settings()
    if settings.embedding_provider == "google":
        return await _embed_google(texts, settings)
    raise EmbeddingError(f"embedding_provider {settings.embedding_provider!r} is not implemented")


async def embed_text(text: str, *, settings: Settings | None = None) -> list[float]:
    """Single-text convenience wrapper — `app/tools/knowledge_tools.py` uses
    this to embed one query, `embed_texts` batches many chunks at once."""
    vectors = await embed_texts([text], settings=settings)
    return vectors[0] if vectors else []


async def _embed_google(texts: list[str], settings: Settings) -> list[list[float]]:
    if not settings.google_api_key:
        raise EmbeddingError("GOOGLE_API_KEY is unset — required for embeddings")

    client = _client(settings)
    out: list[list[float]] = []
    for start in range(0, len(texts), _MAX_BATCH):
        out.extend(await _embed_batch_google(client, texts[start : start + _MAX_BATCH], settings))
    return out


async def _embed_batch_google(
    client: httpx.AsyncClient, batch: list[str], settings: Settings
) -> list[list[float]]:
    model_name = f"models/{settings.embedding_model}"
    body = {
        "requests": [
            {
                "model": model_name,
                "content": {"parts": [{"text": text}]},
                "outputDimensionality": settings.embedding_dimensions,
            }
            for text in batch
        ]
    }

    response = await _post_with_retry(client, f"/{model_name}:batchEmbedContents", body)
    if response.status_code >= 400:
        # Logged with detail, never re-raised verbatim — same "raw provider
        # text never leaks upstream" rule app/tools/booking/calcom.py follows.
        logger.warning("embedding request %d: %s", response.status_code, response.text[:300])
        raise EmbeddingError(f"embedding request failed ({response.status_code})")

    data = response.json()
    embeddings = data.get("embeddings") or []
    if len(embeddings) != len(batch):
        raise EmbeddingError(
            f"embedding response returned {len(embeddings)} vectors for {len(batch)} inputs"
        )
    return [e.get("values") or [] for e in embeddings]


async def _post_with_retry(
    client: httpx.AsyncClient, url: str, body: dict[str, Any]
) -> httpx.Response:
    try:
        response = await client.post(url, json=body)
    except httpx.TimeoutException as exc:
        raise EmbeddingError("embedding request timed out") from exc
    except httpx.HTTPError as exc:
        raise EmbeddingError(f"embedding request failed: {exc}") from exc

    if response.status_code == 429:
        logger.warning("embedding request rate-limited — retrying once")
        try:
            response = await client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding retry failed: {exc}") from exc
    return response


def _client(settings: Settings) -> httpx.AsyncClient:
    # The API key is baked into the client's default headers here (unlike
    # the Supabase case, where a rotating JWT rides per-request instead) —
    # it MUST be part of the cache key, or a rotated key would silently
    # keep reusing a client built with the old one. Fingerprint, never the
    # raw key, in the key string — this ends up in shared_async_client's
    # module-global dict and in tracebacks.
    fingerprint = hashlib.sha256((settings.google_api_key or "").encode()).hexdigest()[:12]
    key = f"embeddings:{settings.google_api_base}:{fingerprint}"
    return shared_async_client(
        key,
        base_url=settings.google_api_base,
        headers={"x-goog-api-key": settings.google_api_key or ""},
        timeout=settings.embedding_timeout_seconds,
    )
