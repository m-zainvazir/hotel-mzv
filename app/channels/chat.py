"""Chat channel — `POST /chat/session` handshake + `POST /chat`, SSE (plan §8).

A thin adapter: it turns an HTTP request into a brain turn and re-encodes
`BrainEvent`s as server-sent events. No business logic lives here.

Three invariants worth stating out loud, mirroring `app/channels/vapi_llm.py`'s
docstring — chat carries the same obligations now that a browser can reach it:

1. **Only an explicit allowlist of events reaches the browser.** `tool_start` /
   `tool_result` carry raw tool args and output — logged only, never sent.
2. **Never trust the body for tenancy on the public path.** A widget caller's
   tenant and session id come from its verified session token
   (`app/channels/widget_auth.py`), minted by `/chat/session` from a public
   widget key. A `tenant_id`/`widget_key` in the body is honoured only for the
   *trusted* (server-to-server) caller — see `require_chat_caller`.
3. **A store failure must never kill a live stream.** Transcript persistence
   (Step 6) is best-effort and backgrounded — the same "never raise
   mid-stream" posture as voice. Transcripts are recorded for the widget path
   only: a `ChatSession` row is guaranteed to exist (created at the
   handshake) before any `ChatMessage` references it by `session_id`, which
   the trusted (server-to-server) path has no equivalent step for.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.brain.runner import BrainEvent, stream_turn
from app.channels.ratelimit import enforce_chat_rate_limit, enforce_session_rate_limit
from app.channels.security import ChatAuth, require_chat_caller
from app.channels.widget_auth import mint_session_token, new_session_id
from app.config import get_settings
from app.db.factory import get_store
from app.db.models import ChatMessage, ChatSession
from app.flows.resolver import resolve_menu
from app.tenancy.loader import get_tenant_config, require_channel_enabled, resolve_tenant_id
from app.tenancy.models import TenantConfig
from app.tenancy.repository import ChannelDisabledError, TenantNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

#: Sent to the browser; everything else (tool_start/tool_result) is internal.
_PUBLIC_EVENT_TYPES = frozenset(
    {"token", "acknowledgement", "suggestions", "handoff", "actions", "cards", "final", "error"}
)

#: Keeps proxies from killing an idle SSE connection while a slow tool runs
#: (Cal.com/Twilio calls can take several seconds — see calcom_timeout_seconds).
HEARTBEAT_INTERVAL_SECONDS = 15.0


def _is_own_origin(origin: str | None) -> bool:
    """True when the request came from a page this app itself serves.

    `chat.allowed_origins` exists to stop an arbitrary third-party site
    embedding a tenant's widget key. Our own hosted "Share agent" page
    (`GET /bot/{widget_key}`) is not that — it's the same widget on our own
    domain, reached by a link the operator deliberately handed out. Without
    this, turning on `allowed_origins` for a real client would silently
    break their share link, which is a confusing failure a long way from
    its cause.
    """
    base = get_settings().public_base_url
    return bool(origin and base and origin.rstrip("/") == base.rstrip("/"))


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    #: Only honoured on the trusted path — a widget caller's session id comes
    #: from its token. Defaults to "web" for backward-compatible direct callers.
    session_id: str = "web"
    tenant_id: str | None = None
    widget_key: str | None = None
    #: Phase 9.2 — a clicked button's payload, currently only `flow:<id>`.
    #: No new trust surface: the id is resolved against *this caller's own
    #: tenant's* config (`app/brain/runner.py`), so a forged value can only
    #: ever reach a flow that tenant already published, and an unknown one
    #: falls through to an ordinary model turn.
    postback: str | None = Field(default=None, max_length=200)


class ChatSessionRequest(BaseModel):
    widget_key: str = Field(min_length=1)


class ChatSessionService(BaseModel):
    slug: str
    name: str
    duration_minutes: int
    price_usd: float | None = None


class ChatSessionTenant(BaseModel):
    name: str
    greeting: str
    accent_color: str
    launcher_label: str
    quick_replies: bool
    services: list[ChatSessionService]
    #: Phase 9.2 — the persistent menu, already resolved to wire rows. Empty
    #: for a tenant with no `chat.menu_flow`, in which case the widget falls
    #: back to today's `services` chips and nothing changes. Additive to the
    #: handshake shape, which widget/README.md explicitly declares free to
    #: change (the `<script>` tag is the frozen contract, not this).
    menu: list[dict] = Field(default_factory=list)
    #: Run one real turn as the panel opens instead of showing `greeting`.
    #: The model can't produce buttons without a turn to produce them in, so
    #: this is what lets an opening menu come from the AI prompt rather than
    #: from `chat.menu_flow`. Suppressed when a `menu` already exists —
    #: that's configured, instant and free, so there's nothing to buy.
    opening_turn: bool = False


class ChatSessionResponse(BaseModel):
    session_id: str
    token: str
    expires_in: int
    tenant: ChatSessionTenant


async def start_session(
    tenant: TenantConfig,
    *,
    widget_key: str = "",
    origin: str | None = None,
    variant: Literal["live", "draft"] = "live",
) -> ChatSessionResponse:
    """Mint a session token + persist a `ChatSession` row, and shape the
    response `/chat/session` returns. Factored out so `/test/session`
    (`app/main.py`, Phase 9.1) can return the exact same shape without a
    widget key — it authenticates via a signed test-link token instead
    (`app/channels/test_links.py`), never a `chat.allowed_origins` check
    (the test page is same-origin, so that boundary doesn't apply).

    `tenant` drives the display metadata below (greeting, accent, services)
    — the caller is responsible for resolving the right one (draft-effective
    for a "Preview draft" link, always-live otherwise). `variant` is
    separate: it's what gets signed into the session token, which is what
    actually makes every subsequent `/chat` turn re-read the tenant's
    *current* draft (`app/brain/runner.py::stream_turn`) rather than a
    snapshot frozen at handshake time.
    """
    session_id = new_session_id()
    token = mint_session_token(tenant.tenant_id, session_id, variant=variant)
    menu = resolve_menu(tenant)

    try:
        await get_store().astart_chat_session(
            ChatSession(
                id=session_id,
                tenant_id=tenant.tenant_id,
                widget_key=widget_key,
                origin=origin,
            )
        )
    except Exception:
        # The handshake must still succeed — a widget with no durable
        # transcript is far better than a widget that can't open at all.
        logger.exception("failed to persist chat_sessions row for %s", session_id)

    return ChatSessionResponse(
        session_id=session_id,
        token=token,
        expires_in=get_settings().widget_session_ttl_seconds,
        tenant=ChatSessionTenant(
            name=tenant.name,
            greeting=tenant.chat.greeting or tenant.greeting,
            accent_color=tenant.chat.accent_color,
            launcher_label=tenant.chat.launcher_label,
            quick_replies=tenant.chat.quick_replies,
            menu=menu,
            opening_turn=tenant.ui.opening_turn and not menu,
            services=[
                ChatSessionService(
                    slug=s.slug,
                    name=s.name,
                    duration_minutes=s.duration_minutes,
                    price_usd=s.price_usd,
                )
                for s in tenant.services
            ],
        ),
    )


@router.post("/chat/session", dependencies=[Depends(enforce_session_rate_limit)])
async def chat_session(
    payload: ChatSessionRequest, origin: str | None = Header(default=None)
) -> ChatSessionResponse:
    """Public handshake: a widget key in, a session-scoped token out.

    Resolves the tenant *before* anything streams, so an unknown widget key is
    a clean 404 — never a truncated stream after `200 OK` has already been sent.
    """
    try:
        tenant_id = resolve_tenant_id(widget_key=payload.widget_key)
    except TenantNotFoundError as exc:
        raise HTTPException(status_code=404, detail="unknown widget key") from exc

    tenant = get_tenant_config(tenant_id)
    try:
        require_channel_enabled(tenant, "chat")
    except ChannelDisabledError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    allowed_origins = tenant.chat.allowed_origins
    if allowed_origins and origin not in allowed_origins and not _is_own_origin(origin):
        raise HTTPException(status_code=403, detail="origin not allowed for this widget key")

    return await start_session(tenant, widget_key=payload.widget_key, origin=origin)


@router.post("/chat", dependencies=[Depends(enforce_chat_rate_limit)])
async def chat(
    request: ChatRequest, auth: ChatAuth = Depends(require_chat_caller)
) -> StreamingResponse:
    persist_as: tuple[str, str] | None = None
    tenant_config_variant: Literal["live", "draft"] = "live"
    if auth.mode == "widget":
        assert auth.claims is not None
        tenant_id: str | None = auth.claims.tenant_id
        widget_key = None
        session_id = auth.claims.session_id
        persist_as = (tenant_id, session_id)
        # Only a verified widget-session token can ever carry "draft" — the
        # trusted, body-driven path below has no equivalent claim to read,
        # so it's always "live", the same as before this feature existed.
        tenant_config_variant = auth.claims.variant
    else:
        tenant_id = request.tenant_id
        widget_key = request.widget_key
        session_id = request.session_id

    try:
        require_channel_enabled(
            get_tenant_config(resolve_tenant_id(tenant_id=tenant_id, widget_key=widget_key)),
            "chat",
        )
    except ChannelDisabledError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TenantNotFoundError:
        # Unrelated to the channel flag — let stream_turn's own resolution
        # (and its blanket try/except -> streamed error event) handle it,
        # exactly as before this check existed. A widget caller can't reach
        # this branch (its tenant_id came from a verified token), so this
        # only matters for the trusted, body-driven path.
        pass

    turn = stream_turn(
        text=request.message,
        tenant_id=tenant_id,
        widget_key=widget_key,
        session_id=session_id,
        channel="chat",
        tenant_config_variant=tenant_config_variant,
        postback=request.postback,
    )

    return StreamingResponse(
        _events(turn, persist_as=persist_as, user_text=request.message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


#: Marks the end of the brain's event stream on the internal queue below.
_SENTINEL = object()


async def _record_chat_message(tenant_id: str, session_id: str, role: str, content: str) -> None:
    """Best-effort transcript write. Never raises — a failed write must never
    surface as a broken turn (see the module docstring's third invariant)."""
    try:
        await get_store().arecord_chat_message(
            ChatMessage(tenant_id=tenant_id, session_id=session_id, role=role, content=content)
        )
    except Exception:
        logger.exception(
            "failed to persist chat message tenant=%s session=%s role=%s",
            tenant_id,
            session_id,
            role,
        )


async def _events(
    turn: AsyncIterator[BrainEvent],
    *,
    persist_as: tuple[str, str] | None = None,
    user_text: str | None = None,
) -> AsyncIterator[str]:
    """Re-encode the brain's events as SSE, with a heartbeat while it thinks.

    The brain is drained by a background task into a queue rather than by
    wrapping `turn.__anext__()` directly in `asyncio.wait_for`: cancelling an
    async generator's `__anext__()` mid-await (which a timeout does) can lose
    the value it was about to yield or corrupt its internal state. Reading
    with a timeout off a queue instead means the heartbeat timer never
    touches — or risks breaking — the graph's own streaming iterator.

    `persist_as` is `(tenant_id, session_id)` for a widget caller (whose
    `ChatSession` row is guaranteed to exist from the handshake) and `None`
    otherwise — see the module docstring's third invariant. Writes are fired
    as background tasks so persistence never sits on the token-streaming
    critical path; they're drained before the generator finishes so nothing
    is silently dropped when the response closes.
    """
    queue: asyncio.Queue[BrainEvent | object] = asyncio.Queue()
    writes: list[asyncio.Task] = []
    #: One INFO line per turn naming every non-token event actually sent.
    #: Added after a live "the carousel doesn't render" report where the
    #: server, the SSE parser, the component and the CSS all checked out
    #: individually and there was still no way to tell whether a given
    #: browser turn had been sent a `cards` frame at all. Cheap (one line,
    #: no payloads, no PII) and it settles that question immediately.
    sent: list[str] = []

    if persist_as is not None and user_text:
        writes.append(asyncio.create_task(_record_chat_message(*persist_as, "user", user_text)))

    async def _pump() -> None:
        try:
            async for event in turn:
                await queue.put(event)
        finally:
            await queue.put(_SENTINEL)

    pump_task = asyncio.create_task(_pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue

            if item is _SENTINEL:
                break
            event = item
            assert isinstance(event, BrainEvent)

            if event.type == "final" and persist_as is not None and event.text:
                writes.append(
                    asyncio.create_task(_record_chat_message(*persist_as, "assistant", event.text))
                )

            if event.type not in _PUBLIC_EVENT_TYPES:
                # tool_start/tool_result carry raw tool args/output — the
                # voice channel filters these too (app/channels/vapi_llm.py).
                # Logged only, never sent to the browser.
                logger.debug("chat tool event %s: %s", event.type, event.tool)
                continue

            if event.type != "token":
                sent.append(event.type)

            payload = {"type": event.type, "text": event.text}
            if event.tool:
                payload["tool"] = event.tool
            if event.data:
                payload["data"] = event.data
            yield f"data: {json.dumps(payload)}\n\n"
    finally:
        if not pump_task.done():
            pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump_task
        if writes:
            await asyncio.gather(*writes, return_exceptions=True)
        logger.info("chat turn sent: %s", ", ".join(sent) or "(nothing)")

    yield "data: [DONE]\n\n"
