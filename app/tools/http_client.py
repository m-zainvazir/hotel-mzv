"""Shared, cached httpx clients for outbound provider calls (Phase 3).

Booking/notifier providers are constructed fresh on every tool call
(`app/tools/providers.py`), so a client built inside `__init__` would pay a
cold TLS handshake on every `check_availability` — straight out of the §13
latency budget. Callers ask for a client here instead, keyed by a string that
must include a credential fingerprint so a test that swaps credentials
(`monkeypatch.setenv`) never reuses a client built with the old ones.
"""

from __future__ import annotations

import httpx

_clients: dict[str, httpx.AsyncClient] = {}


def shared_async_client(
    key: str,
    *,
    base_url: str,
    headers: dict[str, str] | None = None,
    timeout: float,
    auth: httpx.Auth | tuple[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Return the memoised client for `key`, building it on first use."""
    client = _clients.get(key)
    if client is not None:
        return client
    client = httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=timeout,
        auth=auth,
        transport=transport,
    )
    _clients[key] = client
    return client


async def close_shared_clients() -> None:
    """Call on process shutdown so connection pools close cleanly."""
    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        await client.aclose()


def reset_shared_clients() -> None:
    """Test hook — drop cached clients without awaiting a close.

    Tests run each with their own event loop; a client's connection pool is
    bound to the loop it was created on, so a client cached across tests can
    raise "Event loop is closed" on the next request. Dropping the reference
    here (rather than closing it) avoids needing an event loop to do it.
    """
    _clients.clear()
