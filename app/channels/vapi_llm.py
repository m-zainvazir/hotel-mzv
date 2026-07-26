"""Voice channel — OpenAI-compatible `/chat/completions` shim for Vapi.

Vapi's Custom-LLM mode calls this on every turn and starts speaking on partial
text, so this file is a re-encoder and nothing else: it turns a Vapi request
into a brain turn, and `BrainEvent`s into OpenAI SSE chunks. To Vapi it looks
like an LLM; internally it's the same graph the chat widget drives.

Three invariants worth stating out loud, because breaking any of them is
silent on your machine and obvious on a live call:

1. **Only spoken events become audio.** `tool_start` / `tool_result` carry raw
   tool output — leak them into `delta.content` and the caller hears
   "slot_start_iso equals two thousand twenty six dash".
2. **Never raise mid-stream.** An exception after the first chunk leaves the
   caller in silence. Errors become a spoken apology and a clean stop.
3. **Never trust the body for tenancy.** The tenant comes from the assistant id
   or the dialled number, never from a field a caller could set.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from app.brain.runner import stream_turn, thread_is_cold
from app.channels import openai_compat as oai
from app.channels import vapi_schema
from app.channels.security import require_vapi_secret
from app.channels.vapi_schema import VapiChatRequest, VapiMessage
from app.tenancy.loader import resolve_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])

#: Said aloud when the brain fails outright. Silence is the worst outcome.
FALLBACK_LINE = "Sorry, I'm having a bit of trouble hearing you. Could you say that again?"

#: Our share of the 600-800ms end-to-end budget (plan §13). Vapi owns
#: endpointing, STT, TTS and the network legs; this is request-in to audio-out.
FIRST_TOKEN_BUDGET_MS = 400.0


def _log_first_token(received_at: float) -> None:
    elapsed_ms = (time.perf_counter() - received_at) * 1000
    if elapsed_ms > FIRST_TOKEN_BUDGET_MS:
        logger.warning(
            "first token %.0fms — over the %.0fms budget (plan §13)",
            elapsed_ms,
            FIRST_TOKEN_BUDGET_MS,
        )
    else:
        logger.info("first token %.0fms", elapsed_ms)


@router.post("/chat/completions", dependencies=[Depends(require_vapi_secret)])
async def chat_completions(request: Request):
    """Custom-LLM endpoint. Streams unless Vapi explicitly asks not to."""
    received_at = time.perf_counter()
    payload = VapiChatRequest.model_validate(await _json_body(request))

    tenant_id = resolve_tenant_id(
        assistant_id=payload.assistant_id,
        phone_number=payload.dialled_number,
    )
    session_id = f"vapi:{payload.call_id or 'web'}"
    text = payload.latest_user_text()

    # Vapi resends the full transcript every turn, but our checkpointer already
    # holds it — plus the ToolMessages Vapi never sees. Reseed only when the
    # thread is cold (first turn, or a restart mid-call).
    history: list[AnyMessage] | None = None
    if await thread_is_cold(tenant_id, session_id):
        history = _to_langchain(payload.prior_messages())

    logger.info(
        "vapi turn tenant=%s call=%s stream=%s cold=%s",
        tenant_id,
        payload.call_id,
        payload.stream,
        history is not None,
    )

    turn = stream_turn(
        text=text,
        tenant_id=tenant_id,
        session_id=session_id,
        channel="voice",
        called_number=payload.dialled_number,
        history=history,
    )

    if not payload.stream:
        return JSONResponse(oai.completion(oai.new_completion_id(), await _collect(turn)))

    return StreamingResponse(
        _sse_chunks(turn, received_at=received_at),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _sse_chunks(turn, *, received_at: float | None = None) -> AsyncIterator[str]:
    """Map the brain's event stream onto OpenAI chunks."""
    completion_id = oai.new_completion_id()
    yield oai.sse(oai.chunk(completion_id, role="assistant"))

    spoke = False
    #: Set when `escalate` signals a live handoff is warranted. Buffered
    #: rather than emitted immediately: Vapi acts on the transfer frame the
    #: instant it arrives, so sending it here would cut the caller off
    #: mid-sentence. Emitting it after every spoken chunk — once the "I'm
    #: putting you through now" line has actually been said — is what makes
    #: it a *warm* transfer instead of an abrupt one.
    pending_transfer: str | None = None
    try:
        async for event in turn:
            if event.is_spoken and event.text:
                if not spoke and received_at is not None:
                    # Our slice of the §13 budget: request in → first audio out.
                    # Everything else (endpointing, STT, TTS, network) is Vapi's.
                    _log_first_token(received_at)
                spoke = True
                yield oai.sse(oai.chunk(completion_id, content=event.text))
            elif event.type == "error":
                # Say something rather than dropping the call into silence.
                logger.error("brain error on voice turn: %s", event.text)
                if not spoke:
                    spoke = True
                    yield oai.sse(oai.chunk(completion_id, content=FALLBACK_LINE))
            elif event.type == "handoff":
                # An escalation happened on every channel; only voice ever
                # turns it into a live Vapi transfer, and only when the
                # escalator actually declared one possible (`data["transfer"]`
                # — see `_handoff_artifact` in app/brain/runner.py). Chat gets
                # the same event for a click-to-call button, never a frame.
                destination = event.data.get("destination")
                if destination and event.data.get("transfer"):
                    pending_transfer = destination
                    logger.warning("transfer requested to %s", destination)
            elif event.type in ("tool_start", "tool_result", "suggestions"):
                # Deliberately not spoken — see invariant 1 in the module docstring.
                logger.debug("tool %s: %s", event.type, event.tool)
    except Exception:
        logger.exception("vapi stream failed")
        if not spoke:
            spoke = True
            yield oai.sse(oai.chunk(completion_id, content=FALLBACK_LINE))

    if not spoke:
        yield oai.sse(oai.chunk(completion_id, content=FALLBACK_LINE))

    if pending_transfer:
        yield oai.sse(vapi_schema.transfer_call_chunk(pending_transfer))

    yield oai.sse(oai.chunk(completion_id, finish_reason="stop"))
    yield oai.DONE


async def _collect(turn) -> str:
    """Non-streaming path — used only when Vapi sets `stream: false`.

    There is no way to signal a transfer in a single non-streamed completion,
    so one is logged and otherwise dropped. Vapi only uses this path for
    probes, never for a live call.
    """
    parts: list[str] = []
    async for event in turn:
        if event.is_spoken and event.text:
            parts.append(event.text)
        elif event.type == "error" and not parts:
            parts.append(FALLBACK_LINE)
        elif event.type == "handoff":
            logger.warning("transfer requested on a non-streaming turn — cannot signal it here")
    return "".join(parts) or FALLBACK_LINE


async def _json_body(request: Request) -> dict:
    """Parse the body, or 400. A 500 here would be a silent call failure."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="expected a JSON body"
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="expected a JSON object"
        )
    return body


def _to_langchain(messages: list[VapiMessage]) -> list[AnyMessage]:
    converted: list[AnyMessage] = []
    for message in messages:
        content = message.content or ""
        if message.role == "user":
            converted.append(HumanMessage(content=content))
        elif message.role == "assistant":
            converted.append(AIMessage(content=content))
    return converted
