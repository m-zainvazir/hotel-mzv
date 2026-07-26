"""Vapi server events — call lifecycle, transcripts, end-of-call reports.

Another thin adapter. Its whole job is: authenticate, work out which tenant the
event belongs to, and persist. No conversation logic.

Unknown message types return 200 and do nothing on purpose. Vapi adds event
types over time, and an unrecognised one is not an error — 4xx-ing them would
make their dashboard show delivery failures for events we simply don't use.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.channels.security import require_vapi_secret
from app.channels.vapi_schema import redacted
from app.db.factory import get_store
from app.db.models import Call
from app.tenancy.loader import resolve_tenant_id
from app.tenancy.repository import TenantNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

HANDLED = "end-of-call-report"


@router.post("/webhooks/vapi", dependencies=[Depends(require_vapi_secret)])
async def vapi_events(request: Request) -> dict[str, str]:
    try:
        body = await request.json()
    except Exception:
        logger.warning("vapi webhook with a non-JSON body")
        return {"status": "ignored", "type": ""}

    message = (body or {}).get("message") or {}
    event_type = message.get("type", "")

    if event_type != HANDLED:
        logger.debug("ignoring vapi event type=%s", event_type)
        return {"status": "ignored", "type": event_type}

    call = message.get("call") or {}
    tenant_id = _resolve_tenant(call, message)
    if tenant_id is None:
        # Redacted: Vapi puts the tenant's Twilio credentials in this payload.
        logger.warning("end-of-call-report for unknown tenant: %s", redacted(call))
        return {"status": "unmatched", "type": event_type}

    record = await get_store().arecord_call(
        Call(
            tenant_id=tenant_id,
            provider_call_id=str(call.get("id") or message.get("callId") or "unknown"),
            from_number=(call.get("customer") or {}).get("number"),
            to_number=(message.get("phoneNumber") or {}).get("number"),
            started_at=_timestamp(message.get("startedAt")),
            ended_at=_timestamp(message.get("endedAt")),
            duration_seconds=_number(message.get("durationSeconds")),
            ended_reason=message.get("endedReason"),
            transcript=message.get("transcript"),
            recording_url=message.get("recordingUrl"),
            cost_usd=_number(message.get("cost")),
        )
    )

    logger.info(
        "call recorded tenant=%s call=%s duration=%ss reason=%s",
        tenant_id,
        record.provider_call_id,
        record.duration_seconds,
        record.ended_reason,
    )
    return {"status": "recorded", "type": event_type}


def _resolve_tenant(call: dict[str, Any], message: dict[str, Any]) -> str | None:
    try:
        return resolve_tenant_id(
            assistant_id=call.get("assistantId"),
            phone_number=(message.get("phoneNumber") or {}).get("number"),
        )
    except TenantNotFoundError:
        return None


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, int | float):  # epoch millis
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
