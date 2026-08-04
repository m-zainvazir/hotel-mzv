"""app/rag/chunking.py — boundary preference, overlap, oversized input,
empty input, unicode (Phase 9 Part C)."""

from __future__ import annotations

from app.rag.chunking import approx_token_count, chunk_text


def test_empty_input_returns_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\n   \n") == []


def test_short_text_is_a_single_chunk():
    text = "This is a short paragraph. It fits in one chunk easily."
    chunks = chunk_text(text)
    assert chunks == [text]


def test_short_paragraphs_are_packed_together():
    p1 = "First short paragraph."
    p2 = "Second short paragraph."
    chunks = chunk_text(f"{p1}\n\n{p2}", target_tokens=1000)
    assert len(chunks) == 1
    assert p1 in chunks[0]
    assert p2 in chunks[0]


def test_splitting_prefers_paragraph_boundaries():
    """Two paragraphs, each fitting the target on its own but not together,
    must split BETWEEN them rather than cutting either mid-sentence."""
    p1 = "Alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo."
    p2 = "Lima mike november oscar papa quebec romeo sierra tango uniform victor."
    target = (len(p1) // 4) + 2  # just over one paragraph's own token count
    chunks = chunk_text(f"{p1}\n\n{p2}", target_tokens=target)

    assert len(chunks) >= 2
    # Neither paragraph itself got cut mid-sentence — each appears whole
    # somewhere (possibly split from the other, but not internally broken).
    assert any(p1 in c for c in chunks)
    assert any(p2 in c for c in chunks)


def test_an_oversized_paragraph_is_split_into_multiple_chunks():
    long_paragraph = " ".join(f"Sentence number {i} continues on." for i in range(300))
    chunks = chunk_text(long_paragraph, target_tokens=50)
    assert len(chunks) > 1
    assert all(chunk.strip() for chunk in chunks)


def test_an_unsplittable_oversized_token_does_not_crash():
    """A single 'word' (e.g. a long URL) longer than the target — can't be
    split further without corrupting it, so it's allowed to stand on its
    own as an oversized unit rather than raising or truncating data."""
    huge_token = "x" * 5000
    chunks = chunk_text(f"intro. {huge_token} outro.", target_tokens=10)
    assert any(huge_token in c for c in chunks)


def test_consecutive_chunks_overlap():
    words = [f"word{i}" for i in range(600)]
    text = " ".join(words)
    chunks = chunk_text(text, target_tokens=100, overlap_ratio=0.2)
    assert len(chunks) > 1

    tail_words = chunks[0].split()[-5:]
    head_words = chunks[1].split()[:30]
    assert any(w in head_words for w in tail_words), (
        "expected some overlap between consecutive chunks"
    )


def test_zero_overlap_ratio_produces_no_overlap():
    words = [f"word{i}" for i in range(600)]
    text = " ".join(words)
    chunks = chunk_text(text, target_tokens=100, overlap_ratio=0.0)
    assert len(chunks) > 1
    tail_words = set(chunks[0].split()[-5:])
    head_words = set(chunks[1].split()[:5])
    assert not (tail_words & head_words)


def test_no_content_is_lost_across_chunk_boundaries():
    words = [f"tok{i}" for i in range(400)]
    text = " ".join(words)
    chunks = chunk_text(text, target_tokens=80)
    combined = " ".join(chunks)
    for word in words:
        assert word in combined


def test_unicode_text_is_handled_without_error():
    text = (
        ("héllo wörld — 日本語のテキストです。これはテストの文章です。" * 30)
        + "\n\n"
        + ("Второй абзац на русском языке для проверки юникода." * 10)
    )
    chunks = chunk_text(text, target_tokens=50)
    assert chunks
    assert "日本語" in "".join(chunks)
    assert "русском" in "".join(chunks)


def test_approx_token_count_is_at_least_one_and_grows_with_length():
    assert approx_token_count("") == 1
    assert approx_token_count("a") == 1
    short = approx_token_count("a" * 40)
    long = approx_token_count("a" * 400)
    assert long > short
