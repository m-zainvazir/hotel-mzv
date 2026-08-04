"""Text extraction for the knowledge base (Phase 9 Part C).

`.md` / `.txt` / `.csv` need nothing beyond stdlib. `.pdf` (`pypdf`) and
`.docx` (`python-docx`) are the `rag` optional extra (`pyproject.toml`) —
imported lazily inside their own functions so a deploy that never receives
either format never needs the extra installed, the same lazy-import-and-
degrade shape `app/mcp/client.py` uses for `langchain-mcp-adapters`. HTML
uses a stdlib `html.parser.HTMLParser` subclass rather than
`beautifulsoup4` — one more instance of this codebase's "smallest tool that
works" bias.
"""

from __future__ import annotations

import csv
import io
from html.parser import HTMLParser

_TEXT_EXTENSIONS = frozenset({".md", ".txt"})
_HTML_EXTENSIONS = frozenset({".html", ".htm"})


class ExtractionError(RuntimeError):
    """Raised for an unsupported extension or a file that fails to parse —
    never swallowed into empty text, which would silently index nothing."""


def extract_text(filename: str, data: bytes) -> str:
    """Best-effort plain text from `data`, dispatched on `filename`'s
    extension (case-insensitive)."""
    ext = _extension(filename)
    if ext in _TEXT_EXTENSIONS:
        return _decode(data)
    if ext == ".csv":
        return _extract_csv(data)
    if ext == ".pdf":
        return _extract_pdf(data)
    if ext == ".docx":
        return _extract_docx(data)
    if ext in _HTML_EXTENSIONS:
        return html_to_text(_decode(data))
    raise ExtractionError(f"unsupported file extension {ext!r}")


def _extension(filename: str) -> str:
    if "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower()


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _extract_csv(data: bytes) -> str:
    reader = csv.reader(io.StringIO(_decode(data)))
    return "\n".join(", ".join(row) for row in reader)


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ExtractionError(
            'PDF extraction needs the "rag" extra — run `pip install -e ".[rag]"`'
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"could not parse PDF: {exc}") from exc


def _extract_docx(data: bytes) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise ExtractionError(
            'DOCX extraction needs the "rag" extra — run `pip install -e ".[rag]"`'
        ) from exc
    try:
        document = Document(io.BytesIO(data))
        return "\n\n".join(p.text for p in document.paragraphs if p.text.strip())
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"could not parse DOCX: {exc}") from exc


class _TextExtractor(HTMLParser):
    """Concatenates text nodes, dropping `<script>`/`<style>` content
    entirely — a naive text-node walk would otherwise index raw JS/CSS as
    if it were prose."""

    _SKIP_TAGS = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    """Public (not `extract_text`-private) because `app/rag/crawl.py` needs
    the identical stripping behaviour for fetched pages — one implementation,
    not two copies that could drift."""
    parser = _TextExtractor()
    parser.feed(html)
    return "\n".join(parser.parts)
