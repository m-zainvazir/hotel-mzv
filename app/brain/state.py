"""Graph state — channel-agnostic by design (plan §5).

Nothing here names Vapi, Twilio or Groq. A channel adapter's only job is to
fill this in and stream the result back out.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

Channel = Literal["voice", "chat"]


class ReceptionistState(TypedDict, total=False):
    tenant_id: str
    channel: Channel
    messages: Annotated[list[AnyMessage], add_messages]

    emergency: bool
    emergency_reason: str | None

    #: Small, serialisable snapshot of the tenant profile. The full config is
    #: never stored in state — nodes read it from the cached loader instead.
    tenant_summary: dict[str, Any]

    # --- declared by plan §5, not yet written by any node ------------------
    # Kept because LangGraph state is additive and Phase 3 needs them; today
    # the caller's details live in `messages`, which is why booking works
    # without them. Populate these when there's a reason to persist a partial
    # booking (resuming a dropped call), not before.

    #: What we've collected about the caller so far (name, phone, address).
    caller: dict[str, Any]
    #: book | reschedule | cancel | question | quote | human | emergency.
    #: Only `emergency_check` sets this today; there is no router node — the
    #: model routes implicitly via tool choice, to save a hop off the §13 budget.
    intent: str | None
    #: Partial booking under construction.
    booking_draft: dict[str, Any]
