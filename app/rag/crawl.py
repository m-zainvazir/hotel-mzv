"""URL fetch/crawl for the knowledge base (Phase 9 Part C).

Same SSRF concern `plans/phase10.md` item 12 raises for tenant-submitted MCP
server URLs, here for tenant-submitted crawl URLs: refuses to fetch a
private/loopback/link-local/reserved address, resolved via DNS before the
request is made — not just string-matched against the URL's host, so a
hostname that merely *resolves* to an internal address (DNS rebinding) is
caught too. Re-validated again after any redirect, since a 200 to a public
URL can still 302 somewhere private.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from app.rag.extract import html_to_text

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECONDS = 15.0
_MAX_PAGE_BYTES = 5 * 1024 * 1024
_DEFAULT_MAX_PAGES = 20
_DEFAULT_MAX_DEPTH = 2
#: Hard ceiling on the crawl frontier itself, independent of max_pages — a
#: page-limited crawl of a link-dense site could still enqueue thousands of
#: URLs it never visits without this.
_MAX_QUEUED = 500


class CrawlError(RuntimeError):
    pass


@dataclass(frozen=True)
class CrawledPage:
    url: str
    title: str
    text: str


def _is_private_address(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True  # unresolvable — refuse rather than guess
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise CrawlError(f"unsupported URL scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise CrawlError("URL has no host")
    if _is_private_address(parsed.hostname):
        raise CrawlError(f"refusing to fetch a private/internal address: {parsed.hostname!r}")


async def _fetch_raw(client: httpx.AsyncClient, url: str) -> tuple[str, str, str]:
    """Returns `(final_url, body_text, content_type)`. Validates both the
    requested URL and the post-redirect final URL."""
    _validate_url(url)
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise CrawlError(f"could not fetch {url}: {exc}") from exc

    if response.status_code >= 400:
        raise CrawlError(f"{url} returned {response.status_code}")

    final_url = str(response.url)
    _validate_url(final_url)

    if len(response.content) > _MAX_PAGE_BYTES:
        raise CrawlError(f"{url} exceeds the {_MAX_PAGE_BYTES}-byte page limit")

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        raise CrawlError(f"{url} is not HTML/text (content-type: {content_type!r})")

    return final_url, response.text, content_type


async def fetch_page(client: httpx.AsyncClient, url: str) -> CrawledPage:
    """Single-page fetch — `POST /admin/api/tenants/{id}/knowledge/url`
    without the crawl toggle."""
    final_url, body, content_type = await _fetch_raw(client, url)
    if "html" in content_type:
        return CrawledPage(url=final_url, title=_page_title(body), text=html_to_text(body))
    return CrawledPage(url=final_url, title=url, text=body)


async def crawl_site(
    start_url: str,
    *,
    max_pages: int = _DEFAULT_MAX_PAGES,
    max_depth: int = _DEFAULT_MAX_DEPTH,
) -> list[CrawledPage]:
    """Same-domain breadth-first crawl, capped by page count and link depth."""
    _validate_url(start_url)
    domain = urlparse(start_url).hostname

    pages: list[CrawledPage] = []
    seen: set[str] = {start_url}
    queue: list[tuple[str, int]] = [(start_url, 0)]

    async with httpx.AsyncClient(follow_redirects=True, timeout=_FETCH_TIMEOUT_SECONDS) as client:
        while queue and len(pages) < max_pages:
            url, depth = queue.pop(0)
            try:
                final_url, body, content_type = await _fetch_raw(client, url)
            except CrawlError:
                logger.warning("crawl: skipping %s", url, exc_info=True)
                continue

            if "html" not in content_type:
                continue

            pages.append(
                CrawledPage(url=final_url, title=_page_title(body), text=html_to_text(body))
            )

            if depth >= max_depth or len(seen) >= _MAX_QUEUED:
                continue
            for link in _same_domain_links(final_url, body, domain):
                if link not in seen and len(seen) < _MAX_QUEUED:
                    seen.add(link)
                    queue.append((link, depth + 1))

    return pages


# --- tiny stdlib HTML helpers -----------------------------------------------


class _TitleExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title and data.strip():
            self.title = data.strip()


def _page_title(html: str) -> str:
    parser = _TitleExtractor()
    parser.feed(html)
    return parser.title


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.links.append(value)


def _same_domain_links(base_url: str, html: str, domain: str | None) -> list[str]:
    parser = _LinkExtractor()
    parser.feed(html)
    out: list[str] = []
    for href in parser.links:
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme in ("http", "https") and parsed.hostname == domain:
            out.append(parsed._replace(fragment="").geturl())
    return out
