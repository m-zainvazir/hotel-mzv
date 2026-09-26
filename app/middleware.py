"""Request correlation (Phase 7 Step 5).

A raw ASGI middleware, not Starlette's `BaseHTTPMiddleware` — that wrapper
buffers/re-wraps the response, and this app's SSE streams (`/chat`,
`/chat/completions`) are exactly the case where its documented
disconnect-handling problems bite. `app/main.py`'s `CORSMiddleware` is the
same raw-ASGI shape for the same reason; this follows it.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("app.access")

_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def current_request_id() -> str:
    return _request_id_var.get()


class RequestIdFilter(logging.Filter):
    """Attaches the active request id (or `"-"` outside a request) to every
    log record, so both the text and JSON formatters can include it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        return True


#: Paths whose *last segment is a credential*. `/test/{token}` is a signed
#: link that grants a conversation with a tenant's bot until it expires —
#: writing it to an access log copies it into wherever logs are shipped, on
#: every page load. Found while building the 9.3 voice tester: that phase
#: moved its own WebSocket token out of the query string for exactly this
#: reason (uvicorn logs a socket's full path *with* query), and the check
#: turned up the same leak already present on the page the socket is opened
#: from. `/bot/{widget_key}` is deliberately NOT here — a widget key is a
#: public identifier a client pastes into their own HTML, not a secret.
_CREDENTIAL_PREFIXES = ("/test/",)


def redacted_path(path: str) -> str:
    """`/test/<token>` -> `/test/<redacted>`; everything else unchanged.

    Keeps the route identifiable in logs (which is the point of an access
    line) without keeping the secret that makes it usable.
    """
    for prefix in _CREDENTIAL_PREFIXES:
        if path.startswith(prefix) and len(path) > len(prefix):
            return f"{prefix}<redacted>"
    return path


class RequestContextMiddleware:
    """Accepts or generates `X-Request-Id`, echoes it back, and logs one
    access line per request with status and duration — the timing
    `POST /chat` never had (only the voice streaming path had a timer,
    `FIRST_TOKEN_BUDGET_MS`)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id")
        request_id = incoming.decode("latin-1") if incoming else str(uuid.uuid4())
        token = _request_id_var.set(request_id)

        start = time.monotonic()
        status_code = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode("latin-1")),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            logger.info(
                "%s %s -> %s (%.1fms)",
                scope.get("method", ""),
                redacted_path(scope.get("path", "")),
                status_code,
                duration_ms,
            )
            _request_id_var.reset(token)
