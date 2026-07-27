"""Load/latency harness against a *running* server (Phase 7 Step 9).

    python -m scripts.loadtest --endpoint chat --concurrency 5 --turns 20
    python -m scripts.loadtest --endpoint voice --concurrency 1 --scenario booking

Drives `POST /chat` (SSE) or `POST /chat/completions` (the Vapi-shaped
Custom-LLM shim) over real HTTP against an already-running instance of
this app — never the in-process graph directly, since the point is
measuring real network + streaming overhead, the thing nothing else in
this codebase measures. `POST /chat` is entirely uninstrumented otherwise;
`FIRST_TOKEN_BUDGET_MS` (app/channels/vapi_llm.py) is the only existing
timer, and it only covers the voice path.

Reports, per turn: request → first token, and request → the stream's end,
as p50/p95/p99. Pass/fail against `FIRST_TOKEN_BUDGET_MS` is only checked
for `--endpoint voice` — that constant is this app's own share of plan
§13's 600-800ms end-of-speech-to-first-audio budget; Vapi's own STT/TTS/
network legs aren't measurable from here, so a passing number here is
necessary, not sufficient, for the full budget.

`--endpoint chat`'s `final` event carries `data.llm_requests`
(`app/brain/runner.py`'s `TurnCounter`) — genuinely meaningful only at
`--concurrency 1`. `TurnCounter` is a process-wide snapshot difference
(its own docstring explains why — the model call happens inside
LangGraph's own task, so a ContextVar doesn't survive it), so under
overlap it silently counts other concurrent turns' requests too.
Rewriting it is out of scope for this phase; this script just refuses to
report the figure above `--concurrency 1` rather than print a misleading
number.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from dataclasses import dataclass, field

import httpx

#: Mirrors app/channels/vapi_llm.py — this app's own share of plan §13's
#: 600-800ms end-of-speech-to-first-audio budget.
FIRST_TOKEN_BUDGET_MS = 400.0

SCENARIOS: dict[str, list[str]] = {
    "question": [
        "What time is check-in and check-out?",
    ],
    "booking": [
        "I'd like to book a room for two nights starting this Friday.",
        "Two adults please, and yes that works for me.",
        "It's Alex Rivera, phone 555-010-1234, email alex@example.com.",
    ],
    "emergency": [
        "There's a strong smell of gas in my room, I'm scared.",
    ],
}


@dataclass
class TurnResult:
    first_token_ms: float | None
    total_ms: float
    tool_hops: int = 0
    llm_requests: int | None = None
    status_code: int = 200
    error: str | None = None


def _percentile(values: list[float], pct: int) -> float:
    if not values:
        return float("nan")
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[pct - 1]


@dataclass
class Report:
    endpoint: str
    concurrency: int
    results: list[TurnResult] = field(default_factory=list)

    def render(self) -> str:
        ok = [r for r in self.results if r.error is None]
        errors = [r for r in self.results if r.error is not None]
        first_tokens = [r.first_token_ms for r in ok if r.first_token_ms is not None]
        totals = [r.total_ms for r in ok]

        lines = [
            f"endpoint={self.endpoint}  concurrency={self.concurrency}  "
            f"turns={len(self.results)}  ok={len(ok)}  errors={len(errors)}",
        ]
        if first_tokens:
            p50, p95, p99 = (
                _percentile(first_tokens, 50),
                _percentile(first_tokens, 95),
                _percentile(first_tokens, 99),
            )
            lines.append(f"first-token ms   p50={p50:.0f}  p95={p95:.0f}  p99={p99:.0f}")
            if self.endpoint == "voice":
                over = sum(1 for v in first_tokens if v > FIRST_TOKEN_BUDGET_MS)
                verdict = "PASS" if over == 0 else "FAIL"
                lines.append(
                    f"  [{verdict}] {over}/{len(first_tokens)} turns over the "
                    f"{FIRST_TOKEN_BUDGET_MS:.0f}ms budget (plan §13, this app's share)"
                )
        if totals:
            p50, p95, p99 = (
                _percentile(totals, 50),
                _percentile(totals, 95),
                _percentile(totals, 99),
            )
            lines.append(f"turn-total ms    p50={p50:.0f}  p95={p95:.0f}  p99={p99:.0f}")

        tool_hops = [r.tool_hops for r in ok]
        if any(tool_hops):
            lines.append(
                f"tool hops/turn   avg={statistics.mean(tool_hops):.1f}  max={max(tool_hops)}"
            )

        llm_requests = [r.llm_requests for r in ok if r.llm_requests is not None]
        if llm_requests:
            if self.concurrency == 1:
                lines.append(
                    f"llm requests/turn  avg={statistics.mean(llm_requests):.1f}  "
                    f"max={max(llm_requests)}"
                )
            else:
                lines.append(
                    "llm requests/turn  (not reported at concurrency > 1 -- "
                    "TurnCounter is a process-wide snapshot difference, see module docstring)"
                )

        for r in errors[:5]:
            lines.append(f"  error: status={r.status_code} {r.error}")
        if len(errors) > 5:
            lines.append(f"  ... and {len(errors) - 5} more errors")
        return "\n".join(lines)


def _sse_events(text_iter):
    """Parses `data: {...}` lines out of a raw SSE line iterator."""

    async def _gen():
        async for line in text_iter:
            if not line.startswith("data: "):
                continue
            raw = line[len("data: ") :]
            if raw == "[DONE]":
                return
            yield json.loads(raw)

    return _gen()


async def _run_chat_turn(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    tenant_id: str,
    session_id: str,
    message: str,
    api_token: str | None,
) -> TurnResult:
    headers = {"Authorization": f"Bearer {api_token}"} if api_token else {}
    body = {"message": message, "session_id": session_id, "tenant_id": tenant_id}

    start = time.perf_counter()
    first_token_at: float | None = None
    tool_hops = 0
    llm_requests: int | None = None
    try:
        async with client.stream(
            "POST", f"{base_url}/chat", json=body, headers=headers
        ) as response:
            if response.status_code != 200:
                body_text = (await response.aread()).decode(errors="replace")
                return TurnResult(
                    None,
                    (time.perf_counter() - start) * 1000,
                    status_code=response.status_code,
                    error=body_text[:200],
                )
            async for event in _sse_events(response.aiter_lines()):
                event_type = event.get("type")
                if event_type == "token" and first_token_at is None:
                    first_token_at = time.perf_counter()
                elif event_type == "acknowledgement":
                    # Proxy for a tool hop: tool_start/tool_result never reach
                    # the browser by design (app/channels/chat.py), but an
                    # acknowledgement is emitted around exactly that gap
                    # (the acknowledge-then-act rule, CLAUDE.md convention #1).
                    tool_hops += 1
                elif event_type == "final":
                    llm_requests = (event.get("data") or {}).get("llm_requests")
    except (httpx.HTTPError, OSError) as exc:
        return TurnResult(None, (time.perf_counter() - start) * 1000, error=str(exc))

    total_ms = (time.perf_counter() - start) * 1000
    first_token_ms = (first_token_at - start) * 1000 if first_token_at is not None else None
    return TurnResult(first_token_ms, total_ms, tool_hops=tool_hops, llm_requests=llm_requests)


def _vapi_body(tenant, *, call_id: str, messages: list[dict]) -> dict:
    phone_number = tenant.phone_numbers[0] if tenant.phone_numbers else "+15551230000"
    return {
        "model": "ai-receptionist",
        "stream": True,
        "messages": messages,
        "call": {
            "id": call_id,
            "type": "inboundPhoneCall",
            "status": "in-progress",
            "assistantId": tenant.vapi.assistant_id,
            "customer": {"number": "+15557654321"},
            "phoneNumberId": phone_number,
        },
        "phoneNumber": {"id": phone_number, "number": phone_number},
        "customer": {"number": "+15557654321"},
        "metadata": {},
    }


async def _run_voice_turn(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    tenant,
    call_id: str,
    message: str,
    history: list[dict],
    vapi_secret: str | None,
) -> TurnResult:
    headers = {"x-vapi-secret": vapi_secret} if vapi_secret else {}
    messages = [*history, {"role": "user", "content": message}]
    body = _vapi_body(tenant, call_id=call_id, messages=messages)

    start = time.perf_counter()
    first_token_at: float | None = None
    reply_chunks: list[str] = []
    try:
        async with client.stream(
            "POST", f"{base_url}/chat/completions", json=body, headers=headers
        ) as response:
            if response.status_code != 200:
                body_text = (await response.aread()).decode(errors="replace")
                return TurnResult(
                    None,
                    (time.perf_counter() - start) * 1000,
                    status_code=response.status_code,
                    error=body_text[:200],
                )
            async for event in _sse_events(response.aiter_lines()):
                choices = event.get("choices") or []
                if not choices:
                    continue
                content = choices[0].get("delta", {}).get("content")
                if content:
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                    reply_chunks.append(content)
    except (httpx.HTTPError, OSError) as exc:
        return TurnResult(None, (time.perf_counter() - start) * 1000, error=str(exc))

    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": "".join(reply_chunks)})

    total_ms = (time.perf_counter() - start) * 1000
    first_token_ms = (first_token_at - start) * 1000 if first_token_at is not None else None
    return TurnResult(first_token_ms, total_ms)


async def _worker(
    args: argparse.Namespace, tenant, worker_id: int, results: list[TurnResult]
) -> None:
    messages = SCENARIOS[args.scenario]
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        if args.endpoint == "chat":
            session_id = f"loadtest-{worker_id}-{uuid.uuid4().hex[:6]}"
            for turn_index in range(args.turns):
                message = messages[turn_index % len(messages)]
                results.append(
                    await _run_chat_turn(
                        client,
                        args.base_url,
                        tenant_id=tenant.tenant_id,
                        session_id=session_id,
                        message=message,
                        api_token=args.api_token,
                    )
                )
        else:
            call_id = f"loadtest-{worker_id}-{uuid.uuid4().hex[:6]}"
            history: list[dict] = [
                {"role": "system", "content": "You are a voice assistant configured in Vapi."}
            ]
            for turn_index in range(args.turns):
                message = messages[turn_index % len(messages)]
                results.append(
                    await _run_voice_turn(
                        client,
                        args.base_url,
                        tenant=tenant,
                        call_id=call_id,
                        message=message,
                        history=history,
                        vapi_secret=args.vapi_secret,
                    )
                )


async def _main(args: argparse.Namespace) -> None:
    from app.tenancy.loader import get_tenant_config

    tenant = get_tenant_config(args.tenant)
    if args.endpoint == "voice" and not tenant.vapi.assistant_id:
        raise SystemExit(
            f"tenant {args.tenant!r} has no vapi.assistant_id configured -- "
            "run `python -m scripts.provision_vapi --tenant ...` first, or use --endpoint chat"
        )

    results: list[TurnResult] = []
    await asyncio.gather(
        *(_worker(args, tenant, worker_id, results) for worker_id in range(args.concurrency))
    )

    report = Report(endpoint=args.endpoint, concurrency=args.concurrency, results=results)
    print(report.render())


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--endpoint", choices=["chat", "voice"], default="chat")
    parser.add_argument("--tenant", default="hotel-mzv")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--turns", type=int, default=5, help="turns per simulated conversation")
    parser.add_argument("--scenario", choices=list(SCENARIOS), default="question")
    parser.add_argument("--api-token", default=None, help="API_AUTH_TOKEN, for --endpoint chat")
    parser.add_argument(
        "--vapi-secret", default=None, help="VAPI_WEBHOOK_SECRET, for --endpoint voice"
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args(argv)


if __name__ == "__main__":
    asyncio.run(_main(_parse_args()))
