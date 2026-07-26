"""OpenAI chat-completions wire format.

Kept separate from anything Vapi-specific on purpose: Retell, Pipecat and
several other voice orchestrators speak this same dialect, so swapping voice
vendors should touch the layer above this file, never this one.

Reference shape (what the caller's TTS consumes):

    data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"Hi"}}]}

    data: [DONE]
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

DONE = "data: [DONE]\n\n"

#: Reported back as the model name. Vapi only echoes it, but a recognisable
#: value makes call logs on their side readable.
MODEL_NAME = "ai-receptionist"


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def chunk(
    completion_id: str,
    *,
    content: str | None = None,
    role: str | None = None,
    finish_reason: str | None = None,
    model: str = MODEL_NAME,
) -> dict[str, Any]:
    """One `chat.completion.chunk`."""
    delta: dict[str, Any] = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content

    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def completion(completion_id: str, text: str, *, model: str = MODEL_NAME) -> dict[str, Any]:
    """A whole, non-streamed `chat.completion` (for `stream: false` probes)."""
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        # Vapi doesn't bill on these, but clients expect the key to exist.
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def sse(payload: dict[str, Any]) -> str:
    """Encode one SSE frame. The blank line is what terminates the event."""
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
