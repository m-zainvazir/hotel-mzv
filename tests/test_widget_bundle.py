"""Guards against a committed widget/dist/widget.js that has drifted from
widget/src (Phase 5) — the classic failure of committing a build artifact.
Mirrors the hashing done by widget/scripts/buildhash.mjs exactly (same file
set, same sort order, same "relative_path + \\0 + bytes" digest order) so
this catches drift without needing Node in CI.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WIDGET_ROOT = REPO_ROOT / "widget"
WIDGET_SRC_DIR = WIDGET_ROOT / "src"
BUILDHASH_PATH = WIDGET_ROOT / "dist" / ".buildhash"


def _normalise_eol(raw: bytes) -> bytes:
    """CRLF -> LF before hashing.

    Git on Windows checks these sources out with CRLF while storing LF, so
    hashing raw bytes made the digest depend on which machine built the
    bundle: green locally, red on the Linux CI runner reading the identical
    commit, with a message claiming the bundle was stale when it was
    perfectly fresh. The Node side (scripts/buildhash.mjs) normalises
    identically — change one and you must change the other.
    """
    return raw.replace(b"\r\n", b"\n")


def _computed_hash() -> str:
    # Paths are relative to widget/ (e.g. "src/App.tsx"), matching
    # buildhash.mjs's own `relative(ROOT, ...)` where ROOT is the widget/
    # directory the script lives one level under — not the repo root.
    #
    # Sorted by the relative-path STRING, not the Path object: pathlib's
    # ordering is case-insensitive on Windows (matching the filesystem), so
    # sorting Paths directly put "api.ts" before "App.tsx" here while
    # buildhash.mjs's plain Array.sort() (case-sensitive, 'A' < 'a') put them
    # the other way — same file set, different digest. Sorting the string
    # both sides agree on is what makes the two hashes comparable at all.
    files = [p for p in WIDGET_SRC_DIR.rglob("*") if p.is_file()]
    relative_paths = sorted(p.relative_to(WIDGET_ROOT).as_posix() for p in files)

    digest = hashlib.sha256()
    for relative_path in relative_paths:
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_normalise_eol((WIDGET_ROOT / relative_path).read_bytes()))
    return digest.hexdigest()


@pytest.mark.skipif(
    not BUILDHASH_PATH.is_file(),
    reason="widget not built — run `npm --prefix widget install && npm --prefix widget run build`",
)
def test_widget_bundle_matches_its_source():
    committed = BUILDHASH_PATH.read_text(encoding="utf-8").strip()
    assert committed == _computed_hash(), (
        "widget/dist/widget.js (and .buildhash) are stale relative to widget/src — "
        "run `npm --prefix widget run build` and commit the result"
    )
