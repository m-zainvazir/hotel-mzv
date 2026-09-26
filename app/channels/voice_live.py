"""`/voice/live` — the voice tester's WebSocket (Phase 9.3).

Transport only. Every decision about what a turn *is* lives in
`app/voice/session.py`; this file accepts a socket, authenticates it, moves
frames in both directions and owns the closing handshake. The same division
`app/channels/vapi_llm.py` has against `app/brain/runner.py`, and for the
same reason: business logic in a channel adapter is the one thing CLAUDE.md's
core principle forbids outright.

**Auth is the first frame, not a query parameter.** The obvious design — and
what plans/phase9.3.md's D6 originally specified — is `?token=...`, because a
browser's `new WebSocket()` cannot set an `Authorization` header. It was
changed after checking rather than assuming: uvicorn's access logger writes
the full path *with query string* for every WebSocket connect
(`uvicorn/protocols/websockets/websockets_impl.py`, `get_path_with_query_string`),
and `app/logging_config.py` deliberately propagates `uvicorn.access` into the
app's own structured handler — so a `?token=` would be copied verbatim into
production logs on every connection, exactly the leak `app/mcp/connections.py`
already strips query credentials to avoid. Moving the token into the first
message costs a short window in which an accepted socket is not yet
authenticated; that window is bounded by `voice_auth_timeout_seconds` and by
the per-IP open limit, neither of which needs to know who the caller is.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.channels.ratelimit import voice_socket_retry_after
from app.channels.test_links import TestLinkClaims, verify_test_token
from app.config import get_settings
from app.tenancy.admin import get_draft
from app.tenancy.loader import get_tenant_config, require_channel_enabled
from app.tenancy.repository import ChannelDisabledError, TenantNotFoundError
from app.voice import protocol
from app.voice.providers import VoiceProviderError
from app.voice.session import SessionClosed, VoiceSession, summarise

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])

#: Close codes. 1000/1001 are the standard ones; 4xxx is the application
#: range, and using it means a browser can tell "refused" from "network
#: dropped" without parsing a message it may never receive.
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_RATE_LIMITED = 4429
CLOSE_SESSION_ENDED = 1000

#: How often the read loop wakes up with no traffic, so the session's own
#: time/idle caps can fire on a silent socket. Short enough that a cap is
#: enforced promptly, long enough to be free.
_POLL_SECONDS = 2.0


async def _refuse(websocket: WebSocket, code: int, reason: str) -> None:
    """Say why, then close.

    The `closing` message is best-effort: on a socket that was never accepted
    it can't be sent at all, and that's fine — the close code carries the
    same information. What matters is never closing *silently* on a socket
    the client believes is healthy.
    """
    with contextlib.suppress(Exception):
        await websocket.send_json(protocol.closing(reason))
    with contextlib.suppress(Exception):
        await websocket.close(code=code)
    logger.info("voice socket refused code=%d reason=%s", code, reason)


async def _authenticate(websocket: WebSocket) -> TestLinkClaims | None:
    """Read the first frame and verify it. `None` means the socket has
    already been refused and closed."""
    settings = get_settings()
    try:
        raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=settings.voice_auth_timeout_seconds
        )
    except TimeoutError:
        await _refuse(websocket, CLOSE_UNAUTHORIZED, "no auth message")
        return None
    except (WebSocketDisconnect, RuntimeError):
        return None

    try:
        payload = json.loads(raw)
    except ValueError:
        await _refuse(websocket, CLOSE_UNAUTHORIZED, "malformed auth message")
        return None

    token = payload.get("token") if isinstance(payload, dict) else None
    claims = verify_test_token(token) if isinstance(token, str) and token else None
    if claims is None:
        await _refuse(websocket, CLOSE_UNAUTHORIZED, "invalid or expired test link")
        return None
    if claims.mode != "voice":
        # A chat token must not open a voice socket, and the reverse is
        # enforced by `_resolve_test_mode` in app/main.py. Two modes, two
        # surfaces, neither accepting the other's token.
        await _refuse(websocket, CLOSE_FORBIDDEN, "this link is not a voice test link")
        return None
    return claims


@router.websocket("/voice/live")
async def voice_live(websocket: WebSocket) -> None:
    settings = get_settings()
    client_host = websocket.client.host if websocket.client else "unknown"

    if not settings.voice_live_enabled:
        # Closing before `accept()` makes this a rejected handshake rather
        # than an accepted-then-dropped socket — the WebSocket equivalent of
        # the 404 `ADMIN_ENABLED=false` gives, and for the same reason: "is
        # this exposed?" should be answerable from one env var.
        await websocket.close(code=CLOSE_NOT_FOUND)
        return

    retry_after = voice_socket_retry_after(client_host)
    if retry_after is not None:
        await websocket.close(code=CLOSE_RATE_LIMITED)
        return

    await websocket.accept()
    claims = await _authenticate(websocket)
    if claims is None:
        return

    try:
        tenant = get_tenant_config(claims.tenant_id)
        if claims.variant == "draft":
            draft, _ = await get_draft(claims.tenant_id)
            if draft is not None:
                tenant = draft
        require_channel_enabled(tenant, "voice")
    # Order matters, and not cosmetically: `ChannelDisabledError` *subclasses*
    # `TenantNotFoundError` (the same trick `TenantArchivedError` uses, so
    # every existing handler refuses cleanly with no code change). Catch the
    # general one first and a turned-off channel reports itself as an unknown
    # tenant, which sends an operator looking for a bot that is right there.
    except ChannelDisabledError as exc:
        await _refuse(websocket, CLOSE_FORBIDDEN, str(exc))
        return
    except TenantNotFoundError:
        await _refuse(websocket, CLOSE_NOT_FOUND, "unknown tenant")
        return

    try:
        session = VoiceSession(
            tenant=tenant, transport=_WebSocketTransport(websocket), variant=claims.variant
        )
    except VoiceProviderError as exc:
        # A misconfigured provider is an operator problem, and saying so
        # plainly beats a tester that connects and then never speaks.
        await _refuse(websocket, CLOSE_FORBIDDEN, str(exc))
        return

    logger.info(
        "voice socket open tenant=%s variant=%s session=%s stt=%s tts=%s",
        tenant.tenant_id,
        claims.variant,
        session.session_id,
        settings.voice_stt_provider,
        settings.voice_tts_provider,
    )
    try:
        await session.start()
        await _pump(websocket, session)
    except WebSocketDisconnect:
        logger.info("voice socket disconnected session=%s", session.session_id)
    finally:
        # Before the close frame, always: an orphaned STT socket or a
        # half-finished turn task outlives this coroutine otherwise, and
        # gets torn down by the garbage collector from outside its owning
        # task — the `aclose_calcom_mcp_sessions` failure mode.
        await session.aclose()
        logger.info(
            "voice socket closed tenant=%s session=%s %s",
            tenant.tenant_id,
            session.session_id,
            summarise(session.metrics),
        )
        with contextlib.suppress(Exception):
            await websocket.close(code=CLOSE_SESSION_ENDED)


class _WebSocketTransport:
    """Adapts Starlette's socket to `app/voice/session.py::Transport`.

    Swallows send failures on a closed socket rather than raising into the
    orchestrator: the read loop will notice the disconnect on its own turn,
    and a turn task that explodes mid-sentence because the listener hung up
    logs an exception for something entirely routine.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def send_json(self, payload: dict[str, Any]) -> None:
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await self._websocket.send_json(payload)

    async def send_bytes(self, data: bytes) -> None:
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await self._websocket.send_bytes(data)


async def _pump(websocket: WebSocket, session: VoiceSession) -> None:
    """Read until the client leaves or a cap fires.

    Control frames are text, audio frames are binary (D6) — the split is why
    no per-frame JSON parse happens on the 50-messages-a-second leg.
    """
    while True:
        try:
            message = await asyncio.wait_for(websocket.receive(), timeout=_POLL_SECONDS)
        except TimeoutError:
            # A silent socket still has to be able to hit its caps.
            try:
                session.check_caps()
            except SessionClosed as closed:
                await _refuse(websocket, CLOSE_SESSION_ENDED, closed.reason)
                return
            continue

        if message["type"] == "websocket.disconnect":
            return

        try:
            session.check_caps()
            if (data := message.get("bytes")) is not None:
                await session.feed_audio(data)
            elif (text := message.get("text")) is not None:
                await _handle_text(session, text)
        except SessionClosed as closed:
            await _refuse(websocket, CLOSE_SESSION_ENDED, closed.reason)
            return


async def _handle_text(session: VoiceSession, raw: str) -> None:
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.debug("ignoring malformed voice control frame")
        return
    parsed = protocol.parse_client_message(payload)
    if parsed is None or not parsed.is_known:
        # Unknown types are ignored rather than fatal: this is
        # attacker-reachable input, and one bad frame from a flaky browser
        # must never end a live session.
        return
    await session.handle(parsed)
