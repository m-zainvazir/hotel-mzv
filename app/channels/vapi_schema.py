"""Vapi wire types — every Vapi-shaped assumption in the codebase lives here.

Field names verified against Vapi's own reference implementation
(github.com/VapiAI/example-custom-llm, `app/types/vapi.py`). If Vapi changes
the payload, this file is the only thing that should need editing.

Two deliberate choices:

* **Everything is optional and extras are allowed.** A live phone call must
  never 422 because Vapi added a field or because `metadataSendMode` is set to
  `off` and the `call` object is absent. We degrade, we don't reject.
* **`phoneNumber` carries `twilioAccountSid` / `twilioAuthToken`.** Vapi puts
  live credentials in the request body, so `redacted()` exists and must be used
  for any logging or fixture capture. Never dump a raw payload.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Header carrying `server.secret` verbatim.
SECRET_HEADER = "x-vapi-secret"
#: Header carrying an HMAC-SHA256 of the raw body, keyed with `server.secret`.
SIGNATURE_HEADER = "x-vapi-signature"

#: Keys Vapi may include that must never reach a log or a test fixture.
SENSITIVE_KEYS = frozenset(
    {"twilioAuthToken", "twilioAccountSid", "authToken", "accountSid", "apiKey", "secret"}
)

#: The built-in Vapi tool type for a live call transfer.
TRANSFER_TOOL_TYPE = "transferCall"


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class VapiMessage(_Lenient):
    role: str = ""
    content: str | None = None


class VapiCustomer(_Lenient):
    """The person who rang in."""

    number: str | None = None


class VapiCall(_Lenient):
    id: str | None = None
    orgId: str | None = None
    type: str | None = None
    status: str | None = None
    assistantId: str | None = None
    customer: VapiCustomer | None = None
    phoneNumberId: str | None = None


class VapiPhoneNumber(_Lenient):
    """The number that was dialled — i.e. the tenant's own number."""

    id: str | None = None
    number: str | None = None
    provider: str | None = None


class VapiChatRequest(_Lenient):
    """Body of `POST /chat/completions` in Custom-LLM mode."""

    model: str | None = None
    messages: list[VapiMessage] = Field(default_factory=list)
    stream: bool = True
    temperature: float | None = None
    max_tokens: int | None = None

    call: VapiCall | None = None
    phoneNumber: VapiPhoneNumber | None = None
    customer: VapiCustomer | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    # --- accessors the channel adapter actually uses ----------------------

    @property
    def call_id(self) -> str | None:
        return self.call.id if self.call else None

    @property
    def assistant_id(self) -> str | None:
        return self.call.assistantId if self.call else None

    @property
    def dialled_number(self) -> str | None:
        """The tenant's number. Falls back to metadata for web calls."""
        if self.phoneNumber and self.phoneNumber.number:
            return self.phoneNumber.number
        return None

    @property
    def caller_number(self) -> str | None:
        """The customer's number, when the carrier gave us one."""
        for source in (self.customer, self.call.customer if self.call else None):
            if source and source.number:
                return source.number
        return None

    def latest_user_text(self) -> str:
        """The turn we're being asked to answer."""
        for message in reversed(self.messages):
            if message.role == "user" and (message.content or "").strip():
                return message.content or ""
        return ""

    def prior_messages(self) -> list[VapiMessage]:
        """Everything before this turn's user message.

        Vapi's `system` message is dropped: `reason` renders the tenant's own
        system prompt, and carrying both would let them contradict each other.
        """
        history: list[VapiMessage] = []
        seen_last_user = False
        for message in reversed(self.messages):
            if not seen_last_user and message.role == "user":
                seen_last_user = True
                continue
            if message.role in ("user", "assistant") and (message.content or "").strip():
                history.append(message)
        return list(reversed(history))


def transfer_call_tool(destinations: list[str]) -> dict[str, Any]:
    """The assistant's `model.tools` entry declaring where a live transfer may
    go. Vapi refuses to transfer anywhere not declared here at provisioning
    time — re-running `scripts/provision_vapi.py` after changing a tenant's
    `emergency.escalation_phone` is what keeps this in sync (plan Risk 8)."""
    return {
        "type": TRANSFER_TOOL_TYPE,
        "destinations": [{"type": "number", "number": number} for number in destinations],
    }


def transfer_call_chunk(destination: str) -> dict[str, Any]:
    """The SSE data frame a custom-LLM writes to trigger a transfer.

    Vapi parses this out of the stream and executes the transfer itself — it
    does not call our server back to do so. Not an OpenAI chunk shape, so it
    stays out of `app/channels/openai_compat.py` (vendor-neutral, shared with
    Retell/Pipecat)."""
    return {
        "function_call": {
            "name": TRANSFER_TOOL_TYPE,
            "arguments": {"destination": {"type": "number", "number": destination}},
        }
    }


def redacted(payload: Any) -> Any:
    """Deep-copy a payload with Vapi-supplied credentials stripped.

    Vapi embeds the tenant's Twilio credentials in `phoneNumber`. Anything that
    logs or persists a payload must go through here first.
    """
    if isinstance(payload, dict):
        return {
            key: ("***" if key in SENSITIVE_KEYS else redacted(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redacted(item) for item in payload]
    return payload
