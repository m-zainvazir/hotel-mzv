// Writes dist/.buildhash: a sha256 over every file under src/, in sorted
// relative-path order. tests/test_widget_bundle.py recomputes the identical
// hash in Python (same file set, same sort, same "path + \0 + bytes" digest
// order) and fails if dist/widget.js is stale relative to src/ — a
// committed build artifact drifting from its source is the classic failure
// of this approach, and this is the guard that catches it without needing
// Node in CI.
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

// CRLF -> LF before hashing. Git on Windows checks these files out with
// CRLF while storing LF, so hashing raw bytes made the digest depend on
// which machine ran the build: it matched locally and differed on the Linux
// CI runner reading the same commit. Identical content, different bytes,
// and a red CI complaining the bundle was "stale" when it was perfectly
// fresh. tests/test_widget_bundle.py normalises identically — change one
// and you must change the other.
function normaliseEol(buffer) {
  return Buffer.from(buffer.toString("latin1").replaceAll("\r\n", "\n"), "latin1");
}

const digest = createHash("sha256");
for (const relativePath of relativePaths) {
  digest.update(relativePath, "utf-8");
  digest.update("\0", "utf-8");
  digest.update(normaliseEol(readFileSync(join(ROOT, relativePath))));
}

if (!existsSync(DIST_DIR)) {
  mkdirSync(DIST_DIR, { recursive: true });
}
writeFileSync(OUT_FILE, digest.digest("hex"));
console.log(`wrote ${OUT_FILE} (${relativePaths.length} source files hashed)`);
