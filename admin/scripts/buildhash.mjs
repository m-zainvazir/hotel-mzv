// Writes dist/.buildhash: a sha256 over every file under src/, in sorted
// relative-path order. tests/test_admin_bundle.py recomputes the identical
// hash in Python (same file set, same sort, same "path + \0 + bytes" digest
// order) and fails if dist/ is stale relative to src/ — a committed build
// artifact drifting from its source is the classic failure of this approach,
// and this is the guard that catches it without needing Node in CI.
//
// Mirrors widget/scripts/buildhash.mjs exactly, including the one
// non-obvious detail: sort the relative-path STRING here and on the Python
// side, never a Path/pathlib object. pathlib compares case-insensitively on
// Windows (matching the filesystem), so sorting Path objects there put
// "api.ts" before "App.tsx" while this script's plain Array.sort()
// (case-sensitive, 'A' < 'a') puts them the other way — same file set,
// different digest, unless both sides sort the same plain string.
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const SRC_DIR = join(ROOT, "src");
const DIST_DIR = join(ROOT, "dist");
const OUT_FILE = join(DIST_DIR, ".buildhash");

function collectFiles(dir) {
  let files = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      files = files.concat(collectFiles(full));
    } else {
      files.push(full);
    }
  }
  return files;
}

const relativePaths = collectFiles(SRC_DIR)
  .map((absolute) => relative(ROOT, absolute).replaceAll("\\", "/"))
  .sort();

const digest = createHash("sha256");
for (const relativePath of relativePaths) {
  digest.update(relativePath, "utf-8");
  digest.update("\0", "utf-8");
  digest.update(readFileSync(join(ROOT, relativePath)));
}

if (!existsSync(DIST_DIR)) {
  mkdirSync(DIST_DIR, { recursive: true });
}
writeFileSync(OUT_FILE, digest.digest("hex"));
console.log(`wrote ${OUT_FILE} (${relativePaths.length} source files hashed)`);
