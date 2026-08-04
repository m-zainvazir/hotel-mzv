"""Chunking for the knowledge base (Phase 9 Part C).

Stdlib only — no `langchain-text-splitters`, matching this codebase's
recurring "smallest tool that works" bias elsewhere (`app/mcp/registry.py`'s
~30-line server router, `admin/src/router.ts`'s hand-rolled hash router).
Recursive boundary preference — paragraph, then sentence, then word — packs
whole semantic units together up to a target size rather than cutting
mid-sentence, and consecutive chunks overlap so a fact sitting on a chunk
boundary still appears whole in at least one chunk.
"""

from __future__ import annotations

import re

#: ~800 tokens is the plan's own target — big enough to hold a few complete
#: paragraphs of typical documentation prose, small enough that `top_k` (4
#: by default, `KnowledgeSettings`) chunks fit comfortably in a turn's
#: prompt budget alongside everything else.
_TARGET_TOKENS = 800
_OVERLAP_RATIO = 0.15
#: Rough chars-per-token for English prose. This project has no tokenizer
#: dependency (the reasoning model's own token counting happens providers-
#: side) — good enough for *sizing* chunks, not for exact billing.
_CHARS_PER_TOKEN = 4

_PARAGRAPH_RE = re.compile(r"\n\s*\n+")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def approx_token_count(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def chunk_text(
    text: str,
    *,
    target_tokens: int = _TARGET_TOKENS,
    overlap_ratio: float = _OVERLAP_RATIO,
) -> list[str]:
    """Split `text` into overlapping chunks of roughly `target_tokens` each.

    Empty (or whitespace-only) input returns `[]`, never a single empty
    chunk — callers (`app/rag/ingest.py`) treat that as "nothing to index"
    rather than a document with one blank chunk.
    """
    text = text.strip()
    if not text:
        return []

    target_chars = max(target_tokens * _CHARS_PER_TOKEN, 1)
    overlap_chars = int(target_chars * overlap_ratio)

    units = _split_paragraphs(text, target_chars)
    return _pack(units, target_chars, overlap_chars)


# --- recursive boundary splitting -------------------------------------------


def _split_paragraphs(text: str, limit: int) -> list[str]:
    paragraphs = [p.strip() for p in _PARAGRAPH_RE.split(text) if p.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= limit:
            units.append(paragraph)
        else:
            units.extend(_split_sentences(paragraph, limit))
    return units


def _split_sentences(text: str, limit: int) -> list[str]:
    sentences = [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]
    units: list[str] = []
    for sentence in sentences:
        if len(sentence) <= limit:
            units.append(sentence)
        else:
            units.extend(_split_words(sentence, limit))
    return units


def _split_words(text: str, limit: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    units: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        add_len = len(word) + (1 if current else 0)
        if current and current_len + add_len > limit:
            units.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += add_len
    if current:
        units.append(" ".join(current))
    # A single "word" longer than `limit` (a URL, a hash) can't be split
    # further without breaking it mid-token — let it stand as its own
    # oversized unit rather than truncating data.
    return units


# --- packing with overlap ---------------------------------------------------


def _pack(units: list[str], target_chars: int, overlap_chars: int) -> list[str]:
    if not units:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for unit in units:
        add_len = len(unit) + (2 if current else 0)  # "\n\n" separator
        if current and current_len + add_len > target_chars:
            finished = "\n\n".join(current)
            chunks.append(finished)
            tail = _overlap_tail(finished, overlap_chars)
            current = [tail] if tail else []
            current_len = len(tail)
            add_len = len(unit) + (2 if current else 0)
        current.append(unit)
        current_len += add_len

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _overlap_tail(text: str, overlap_chars: int) -> str:
    """The last `overlap_chars` of `text`, trimmed forward to the next word
    boundary so the overlap never starts mid-word."""
    if overlap_chars <= 0 or len(text) <= overlap_chars:
        return ""
    tail = text[-overlap_chars:]
    space_idx = tail.find(" ")
    return tail[space_idx + 1 :] if space_idx != -1 else tail
