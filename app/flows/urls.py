"""Deciding whether a model-supplied URL may reach a browser.

One implementation, used by everything that renders a link the model wrote
— `offer_actions`' free-form buttons and `offer_cards`' images, card links
and card buttons alike. Two validators that could disagree about what
counts as `amazon.com` is exactly the class of bug this codebase keeps
paying for elsewhere, and here the disagreement would be a security hole
rather than a cosmetic one.

The widget deliberately re-checks none of this. It renders what arrives,
which is only safe because this is the single gate everything passes
through.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Anything else — `javascript:`, `data:`, `vbscript:`, `file:` — is
#: refused unconditionally, no matter how permissive a tenant's settings
#: are. There is no legitimate button that needs one, and an `<a href>` on
#: the *client's own website* is where the damage would land.
SAFE_SCHEMES = ("http", "https")


def host_allowed(url: str, allowed_hosts: list[str]) -> bool:
    """True when `url` is http(s) and its host passes the allowlist.

    An empty allowlist means "any host" — that's the zero-config default,
    where a bot builds its buttons from whatever its prompt tells it. A
    `*.example.com` entry matches subdomains *and* the bare domain, which is
    what an operator typing it means.

    Matching is on a dot boundary, never a bare `endswith`, so an
    `amazon.com` allowlist does not admit `notamazon.com`.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in SAFE_SCHEMES or not parsed.hostname:
        return False
    if not allowed_hosts:
        return True

    host = parsed.hostname.lower().rstrip(".")
    for entry in allowed_hosts:
        pattern = entry.strip().lower().lstrip("*").lstrip(".")
        if not pattern:
            continue
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


def safe_url(url: str | None, allowed_hosts: list[str], *, what: str = "url") -> str | None:
    """`url` if it may be rendered, else None (with a WARNING).

    Never raises. A rejected URL costs a button its link or a card its
    picture; it must never cost the caller their answer — the same "a
    partial answer beats a failed turn" posture the rest of this layer
    takes.
    """
    if not url:
        return None
    if host_allowed(url, allowed_hosts):
        return url
    logger.warning("rejected %s (scheme or host not allowed): %r", what, url[:120])
    return None
