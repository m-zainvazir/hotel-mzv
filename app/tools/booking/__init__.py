"""Booking providers behind one interface."""

from app.tools.booking.base import (
    BookingError,
    BookingProvider,
    BookingRequest,
    Slot,
    SlotUnavailableError,
)
from app.tools.booking.stub import StubBookingProvider

__all__ = [
    "BookingError",
    "BookingProvider",
    "BookingRequest",
    "Slot",
    "SlotUnavailableError",
    "StubBookingProvider",
]
