"""Native confirmation + escalation tools — tier 1 (critical path)."""

from __future__ import annotations

import logging

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.db.factory import get_store
from app.db.supabase_store import SupabaseStoreError
from app.tools.context import channel_from_config, tenant_from_config
from app.tools.formatting import normalize_phone, speakable_datetime
from app.tools.messaging.base import MessagingError
from app.tools.providers import get_escalator, get_notifier

logger = logging.getLogger(__name__)


@tool
async def send_confirmation(
    job_id: str,
    to_phone: str = "",
    config: RunnableConfig = None,
) -> str:
    """Text the caller a written confirmation of a booked job.

    Call this immediately after `book_job` succeeds. `job_id` is the id that
    `book_job` returned. `to_phone` is optional — leave it empty to use the
    number already on the booking.
    """
    tenant = tenant_from_config(config)
    try:
        job = await get_store().aget(tenant.tenant_id, job_id)
    except SupabaseStoreError:
        # A transient store error is NOT "this job doesn't exist" — the
        # booking almost certainly landed. Telling the model that would
        # invite it to book again, duplicating a real reservation.
        logger.exception(
            "could not look up job=%s tenant=%s for confirmation", job_id, tenant.tenant_id
        )
        return (
            f"ERROR: could not look up the booking right now (job_id={job_id}). The "
            f"booking IS confirmed — read the date, time and address out to the caller "
            f"from what you already know. Do not book again."
        )
    if job is None:
        return f"ERROR: no job {job_id!r} for this business. Check the job_id from book_job."

    destination = normalize_phone(to_phone) if to_phone else job.customer_phone
    if destination is None:
        return "ERROR: to_phone is not a usable phone number."

    try:
        body = tenant.notifications.confirmation_template.format(
            business_name=tenant.name,
            service=job.service_name,
            when=speakable_datetime(job.scheduled_start, tenant.tz),
            address=job.address,
            customer_name=job.customer_name,
        )
    except (KeyError, IndexError):
        logger.error("bad confirmation_template for tenant=%s", tenant.tenant_id)
        body = (
            f"{tenant.name}: you're booked for {job.service_name} on "
            f"{speakable_datetime(job.scheduled_start, tenant.tz)}."
        )

    try:
        message = await get_notifier(tenant).send_sms(
            tenant, to=destination, body=body, kind="confirmation"
        )
    except MessagingError:
        logger.exception("confirmation sms failed tenant=%s job=%s", tenant.tenant_id, job.id)
        return (
            f"ERROR: the confirmation text did not send. The booking IS confirmed "
            f"(job_id={job.id}) — read the date, time and address out to the caller "
            f"instead. Do not book again."
        )

    logger.info("confirmation sent tenant=%s job=%s msg=%s", tenant.tenant_id, job.id, message.id)
    return f"SENT confirmation to {destination}: {body}"


@tool(response_format="content_and_artifact")
async def escalate(
    reason: str,
    caller_summary: str = "",
    callback_number: str = "",
    config: RunnableConfig = None,
) -> tuple[str, dict]:
    """Hand the caller to a human on-call immediately.

    Use for genuine emergencies and whenever the caller asks for a person.
    `reason` is one short line on why (e.g. "burning smell from panel").
    `caller_summary` is what you already know — name, address, problem — so the
    human doesn't start from scratch. `callback_number` is where to ring back if
    the call drops.
    """
    tenant = tenant_from_config(config)
    channel = channel_from_config(config)

    normalized = normalize_phone(callback_number) if callback_number else None
    escalator = get_escalator(tenant, channel)
    escalation = await escalator.escalate(
        tenant,
        reason=reason,
        caller_summary=caller_summary,
        callback_number=normalized,
        channel=channel,
    )

    try:
        await get_notifier(tenant).send_sms(
            tenant,
            to=tenant.emergency.escalation_phone,
            body=tenant.emergency.alert_template.format(
                customer_name=caller_summary or "caller",
                customer_phone=normalized or "unknown",
                reason=reason,
            ),
            kind="alert",
        )
    except MessagingError:
        # The escalation itself (and any warm transfer) must stand even when
        # the alert text fails — e.g. an unverified/placeholder on-call
        # number. Losing the SMS is bad; losing the whole escalation is worse.
        logger.exception("alert sms failed tenant=%s — escalation still stands", tenant.tenant_id)

    # Only promise a handoff the escalator can actually perform. A transfer
    # can still fail for reasons we cannot see (destination not reachable,
    # not yet re-provisioned on Vapi — see plan Risk 7/8), so even the
    # transfer branch gives the caller the number to dial themselves.
    if escalator.can_transfer:
        handoff = (
            f"Transferring the caller to {escalation.transferred_to} now. If the line "
            f"drops, tell them to call {escalation.transferred_to} directly."
        )
    else:
        handoff = (
            f"On-call has been alerted at {escalation.transferred_to}. You CANNOT transfer "
            f"this caller — do not say you are putting them through. Tell them to hang up "
            f"and call {escalation.transferred_to} themselves right now, or offer to have "
            f"someone call them back."
        )
    logger.warning(
        "escalated tenant=%s channel=%s transfer=%s reason=%s",
        tenant.tenant_id,
        channel,
        escalator.can_transfer,
        reason,
    )
    text = f"ESCALATED id={escalation.id}. {handoff} Say the safety guidance out loud first."
    artifact = {
        "kind": "handoff",
        "transfer": escalator.can_transfer,
        "destination": escalation.transferred_to,
        "escalation_id": escalation.id,
        "reason": reason,
    }
    return text, artifact
