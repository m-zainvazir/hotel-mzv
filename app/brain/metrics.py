"""Provider request accounting.

Every LLM request is billable, and the count is not obvious from the outside:
one conversational turn costs one request, *plus one per tool hop*, *plus one
per retry*. A four-turn booking conversation can therefore be fifteen-plus
requests. This module makes that visible instead of surprising.
"""

from __future__ import annotations

from threading import Lock

_lock = Lock()
_total = 0
_tokens = 0


def record_llm_request(prompt_tokens: int = 0) -> int:
    """Count one provider request. Returns the process-wide total."""
    global _total, _tokens
    with _lock:
        _total += 1
        _tokens += prompt_tokens
        return _total


def total_llm_requests() -> int:
    return _total


def total_prompt_tokens() -> int:
    return _tokens


def reset_metrics() -> None:
    global _total, _tokens
    with _lock:
        _total = 0
        _tokens = 0


class TurnCounter:
    """Requests issued during one turn.

    A plain snapshot difference rather than a ContextVar: the model call happens
    inside LangGraph's own task, and context set in the caller does not travel
    back out of it — an earlier version of this silently reported zero for every
    turn.
    """

    def __init__(self) -> None:
        self._start = total_llm_requests()

    @property
    def requests(self) -> int:
        return total_llm_requests() - self._start
