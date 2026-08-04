"""Provider factories.

One place decides which concrete implementation a tenant gets. Tools depend on
these factories, never on a vendor module, which is what keeps the brain
provider-agnostic (CLAUDE.md convention #4).
"""

from __future__ import annotations

from app.tenancy.models import TenantConfig
from app.tools.booking.base import BookingProvider
from app.tools.booking.stub import StubBookingProvider
from app.tools.messaging.base import Escalator, Notifier
from app.tools.messaging.stub import StubNotifier

_booking_overrides: dict[str, BookingProvider] = {}
_notifier_override: Notifier | None = None
_escalator_override: Escalator | None = None


def get_booking_provider(tenant: TenantConfig) -> BookingProvider:
    if tenant.tenant_id in _booking_overrides:
        return _booking_overrides[tenant.tenant_id]

    choice = tenant.booking.provider
    if choice == "stub":
        return StubBookingProvider()
    if choice == "calcom":
        from app.tools.booking.calcom import CalcomBookingProvider

        return CalcomBookingProvider()
    if choice == "mcp_calcom":
        from app.tools.booking.mcp_calcom import McpBookingProvider

        return McpBookingProvider()
    if choice == "google":
        from app.tools.booking.google import GoogleCalendarBookingProvider

        return GoogleCalendarBookingProvider()
    raise NotImplementedError(f"booking provider {choice!r} is not wired yet (plan §10)")


def get_notifier(tenant: TenantConfig) -> Notifier:
    if _notifier_override is not None:
        return _notifier_override

    choice = tenant.notifications.provider
    if choice == "stub":
        return StubNotifier()
    if choice == "twilio":
        from app.tools.messaging.twilio import TwilioNotifier

        return TwilioNotifier()
    raise NotImplementedError(f"notifier {choice!r} is not wired yet")


def get_escalator(tenant: TenantConfig, channel: str = "chat") -> Escalator:
    if _escalator_override is not None:
        return _escalator_override

    if (
        channel == "voice"
        and tenant.emergency.allow_warm_transfer
        and tenant.emergency.escalation_phone
    ):
        from app.tools.messaging.transfer import WarmTransferEscalator

        return WarmTransferEscalator()

    from app.tools.messaging.transfer import SmsCallbackEscalator

    return SmsCallbackEscalator()


# --- test / local-dev hooks -------------------------------------------------


def set_booking_provider(tenant_id: str, provider: BookingProvider | None) -> None:
    if provider is None:
        _booking_overrides.pop(tenant_id, None)
    else:
        _booking_overrides[tenant_id] = provider


def reset_provider_overrides() -> None:
    global _notifier_override, _escalator_override
    _booking_overrides.clear()
    _notifier_override = None
    _escalator_override = None
