# optionslens/backend/notify/
"""
Notifications.

`get_dispatcher()` is the single entry point application code should use, so
callers never construct channels directly and never learn what channels exist.
Adding web push or email later means registering another channel here — no
trade-side change.
"""
from typing import Optional

from notify.base import (  # noqa: F401
    Dispatcher, Notification, NotificationChannel, Severity,
)
from notify.telegram import TelegramChannel  # noqa: F401

_dispatcher: Optional[Dispatcher] = None


def get_dispatcher() -> Dispatcher:
    """Process-wide dispatcher, built on first use from the environment."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = Dispatcher(channels=[TelegramChannel()])
    return _dispatcher


def reset_dispatcher():
    """Test helper — forces a rebuild on the next get_dispatcher()."""
    global _dispatcher
    _dispatcher = None
