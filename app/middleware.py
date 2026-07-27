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
                scope.get("path", ""),
                status_code,
                duration_ms,
            )
            _request_id_var.reset(token)
