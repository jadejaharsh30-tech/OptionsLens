# optionslens/backend/notify/base.py
"""
Notification channel interface.

Kept abstract so trade code never imports Telegram. A trade manager that knows
about bot tokens is a trade manager you cannot test without a network, and the
whole point of the signal/trade split is that the interesting logic stays pure.

Dedupe is part of the contract rather than each channel's problem: an alert
engine polling every 10 seconds will re-raise the same condition until it
changes, and a phone that buzzes 40 times for one event trains you to ignore it.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional


class Severity(str, Enum):
    INFO     = "INFO"      # routine: recorder started, daily digest
    SIGNAL   = "SIGNAL"    # a signal fired
    TRADE    = "TRADE"     # entered, exited
    WARNING  = "WARNING"   # data gap, recorder stalled
    CRITICAL = "CRITICAL"  # stop hit, risk breach


@dataclass
class Notification:
    title:    str
    body:     str
    severity: Severity = Severity.INFO
    # Events sharing a dedupe_key within the dedupe window are sent once.
    dedupe_key: Optional[str] = None
    meta:     dict[str, Any] = field(default_factory=dict)

    def format_text(self) -> str:
        icon = {
            Severity.INFO: "•", Severity.SIGNAL: "◆", Severity.TRADE: "▶",
            Severity.WARNING: "!", Severity.CRITICAL: "!!",
        }[self.severity]
        return f"{icon} {self.title}\n\n{self.body}"


class NotificationChannel(ABC):
    """One delivery mechanism."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def configured(self) -> bool:
        """False when credentials are missing — never raise for that."""

    @abstractmethod
    async def send(self, notification: Notification) -> bool: ...


class Dispatcher:
    """
    Fans a notification out to every configured channel, with dedupe.

    Delivery failures are logged and swallowed. A notification channel must
    never be able to take down a trading loop — a missed Telegram message is an
    inconvenience, an exception propagating into position management is not.
    """

    def __init__(self, channels: Optional[list[NotificationChannel]] = None,
                 dedupe_window_sec: int = 300,
                 min_severity: Severity = Severity.INFO):
        self.channels = channels or []
        self.dedupe_window = timedelta(seconds=dedupe_window_sec)
        self.min_severity = min_severity
        self._recent: dict[str, datetime] = {}

    _ORDER = [Severity.INFO, Severity.SIGNAL, Severity.TRADE,
              Severity.WARNING, Severity.CRITICAL]

    def _passes_severity(self, sev: Severity) -> bool:
        return self._ORDER.index(sev) >= self._ORDER.index(self.min_severity)

    def _is_duplicate(self, n: Notification, now: datetime) -> bool:
        if not n.dedupe_key:
            return False
        last = self._recent.get(n.dedupe_key)
        if last and now - last < self.dedupe_window:
            return True
        self._recent[n.dedupe_key] = now
        return False

    async def send(self, notification: Notification) -> dict[str, bool]:
        """Returns per-channel delivery status. Never raises."""
        import logging
        logger = logging.getLogger(__name__)

        now = datetime.now()
        if not self._passes_severity(notification.severity):
            return {}
        if self._is_duplicate(notification, now):
            return {}

        results: dict[str, bool] = {}
        for channel in self.channels:
            if not channel.configured:
                continue
            try:
                results[channel.name] = await channel.send(notification)
            except Exception as e:
                logger.error(f"Notification via {channel.name} failed: {e!r}")
                results[channel.name] = False
        return results

    def add(self, channel: NotificationChannel):
        self.channels.append(channel)

    @property
    def active_channels(self) -> list[str]:
        return [c.name for c in self.channels if c.configured]
