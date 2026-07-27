"""Process output setup — logging and console encoding. Call once, at start."""

from __future__ import annotations

import json
import logging
import sys

from app.config import get_settings
from app.middleware import RequestIdFilter

_configured = False


def force_utf8_console() -> None:
    """Windows consoles still default to cp1252, which can't print '⚙' or '›'."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - exotic terminals
                pass


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    """Install our own handler on the root logger — explicitly, not via
    `logging.basicConfig`, which is a no-op the moment any handler already
    exists on the root logger (a platform wrapper adding one would silently
    void `LOG_LEVEL`).

    Also takes over `uvicorn` / `uvicorn.access` / `uvicorn.error`: uvicorn
    installs its own handlers + formatters on those loggers during its own
    startup (`Config.configure_logging()`, which runs before this app
    module is even imported) — clearing them and letting the records
    propagate to root is what makes every log line share one format,
    including uvicorn's own "Uvicorn running on..." lines. That's also why
    `app/main.py` calls this at import time rather than inside `lifespan`:
    by the time `lifespan` runs, uvicorn has already printed its own
    startup lines in its own format.
    """
    global _configured
    if _configured:
        return

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    if settings.log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-8s %(name)s [%(request_id)s]: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True

    # These are chatty and rarely useful outside deep debugging.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _configured = True


def reset_logging_config() -> None:
    """Test hook — drop the "already configured" guard so a test can call
    `configure_logging()` again under different settings (e.g. `log_format`)."""
    global _configured
    _configured = False
