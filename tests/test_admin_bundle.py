"""Guards against a committed admin/dist that has drifted from admin/src
(Phase 8) — the classic failure of committing a build artifact. Clone of
tests/test_widget_bundle.py; mirrors the hashing done by
admin/scripts/buildhash.mjs exactly (same file set, same sort order, same
"relative_path + \\0 + bytes" digest order) so this catches drift without
needing Node in CI.

There are now two artifacts with this property — skip silently means green
is not proof either bundle is actually built.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ADMIN_ROOT = REPO_ROOT / "admin"
ADMIN_SRC_DIR = ADMIN_ROOT / "src"
BUILDHASH_PATH = ADMIN_ROOT / "dist" / ".buildhash"


def _computed_hash() -> str:
    # Paths are relative to admin/ (e.g. "src/App.tsx"), matching
    # buildhash.mjs's own `relative(ROOT, ...)` where ROOT is the admin/
    # directory the script lives one level under — not the repo root.
    #
    # Sorted by the relative-path STRING, not the Path object — pathlib's
    # ordering is case-insensitive on Windows (matching the filesystem), so
    # sorting Paths directly disagrees with buildhash.mjs's case-sensitive
    # Array.sort() on filenames that only differ by case. Same file set,
    # different digest unless both sides sort the identical string.
    files = [p for p in ADMIN_SRC_DIR.rglob("*") if p.is_file()]
    relative_paths = sorted(p.relative_to(ADMIN_ROOT).as_posix() for p in files)

    digest = hashlib.sha256()
    for relative_path in relative_paths:
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update((ADMIN_ROOT / relative_path).read_bytes())
    return digest.hexdigest()


@pytest.mark.skipif(
    not BUILDHASH_PATH.is_file(),
    reason="admin bundle not built — run `npm --prefix admin install && "
    "npm --prefix admin run build`",
)
def test_admin_bundle_matches_its_source():
    committed = BUILDHASH_PATH.read_text(encoding="utf-8").strip()
    assert committed == _computed_hash(), (
        "admin/dist (and .buildhash) are stale relative to admin/src — "
        "run `npm --prefix admin run build` and commit the result"
    )
