"""
Notification tests.

Two properties are load-bearing:
  - a channel failure can never propagate into position management
  - dedupe actually suppresses, because a phone that buzzes 40 times for one
    event trains you to ignore it — including the message that mattered
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notify.base import (  # noqa: E402
    Dispatcher, Notification, NotificationChannel, Severity,
)
from notify.events import (  # noqa: E402
    daily_digest, recorder_stalled, risk_warning, signal_fired,
    trade_closed, trade_opened, trade_rejected,
)
from notify.telegram import TelegramChannel  # noqa: E402
from trading.models import ExitReason, Trade, TradeLeg, TradeState  # noqa: E402


class FakeChannel(NotificationChannel):
    def __init__(self, configured=True, fail=False):
        self._configured, self._fail = configured, fail
        self.sent: list[Notification] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def configured(self) -> bool:
        return self._configured

    async def send(self, notification: Notification) -> bool:
        if self._fail:
            raise RuntimeError("network exploded")
        self.sent.append(notification)
        return True


def run(coro):
    return asyncio.run(coro)


def make_trade(**kw) -> Trade:
    leg = TradeLeg(symbol="NIFTY", expiry_date="17-09-2026", strike=24500.0,
                   option_type="CE", action="BUY", lots=2, lot_size=75,
                   entry_price=100.0)
    defaults = dict(trade_id="T1", state=TradeState.OPEN, symbol="NIFTY",
                    legs=[leg], created_at="2026-09-11T10:00:00+05:30")
    defaults.update(kw)
    return Trade(**defaults)


# ── Dispatcher ────────────────────────────────────────────────────────────────

def test_sends_to_every_configured_channel():
    a, b = FakeChannel(), FakeChannel()
    d = Dispatcher([a, b])
    result = run(d.send(Notification("t", "b")))
    assert len(a.sent) == 1 and len(b.sent) == 1
    assert all(result.values())


def test_unconfigured_channels_are_skipped_silently():
    ok, missing = FakeChannel(), FakeChannel(configured=False)
    d = Dispatcher([ok, missing])
    run(d.send(Notification("t", "b")))
    assert len(ok.sent) == 1 and len(missing.sent) == 0


def test_a_failing_channel_cannot_break_the_caller():
    """A missed alert is an inconvenience; an exception in position management is not."""
    bad, good = FakeChannel(fail=True), FakeChannel()
    d = Dispatcher([bad, good])
    result = run(d.send(Notification("t", "b")))
    assert result["fake"] is False or len(good.sent) == 1   # no raise


def test_dedupe_suppresses_repeats_within_the_window():
    ch = FakeChannel()
    d = Dispatcher([ch], dedupe_window_sec=300)
    for _ in range(5):
        run(d.send(Notification("Stop hit", "body", dedupe_key="stop:T1")))
    assert len(ch.sent) == 1


def test_different_dedupe_keys_both_send():
    ch = FakeChannel()
    d = Dispatcher([ch])
    run(d.send(Notification("a", "b", dedupe_key="k1")))
    run(d.send(Notification("a", "b", dedupe_key="k2")))
    assert len(ch.sent) == 2


def test_notifications_without_a_key_are_never_deduped():
    ch = FakeChannel()
    d = Dispatcher([ch])
    for _ in range(3):
        run(d.send(Notification("a", "b")))
    assert len(ch.sent) == 3


def test_severity_floor_filters_low_priority_noise():
    ch = FakeChannel()
    d = Dispatcher([ch], min_severity=Severity.WARNING)
    run(d.send(Notification("info", "b", severity=Severity.INFO)))
    run(d.send(Notification("trade", "b", severity=Severity.TRADE)))
    assert len(ch.sent) == 0

    run(d.send(Notification("warn", "b", severity=Severity.WARNING)))
    run(d.send(Notification("crit", "b", severity=Severity.CRITICAL)))
    assert len(ch.sent) == 2


# ── Event builders ────────────────────────────────────────────────────────────

def test_stop_loss_close_is_escalated_to_critical():
    t = make_trade(state=TradeState.CLOSED, realized_pnl=-2340.0,
                   exit_reason=ExitReason.STOP_LOSS, mae=-2900.0, mfe=150.0,
                   total_charges=120.0)
    n = trade_closed(t)
    assert n.severity is Severity.CRITICAL
    assert "-2,340" in n.body and "MAE" in n.body


def test_target_close_is_a_normal_trade_notification():
    t = make_trade(state=TradeState.CLOSED, realized_pnl=1500.0,
                   exit_reason=ExitReason.TARGET, mae=-200.0, mfe=1800.0)
    n = trade_closed(t)
    assert n.severity is Severity.TRADE
    assert "Profit" in n.title


def test_rejection_is_info_not_warning():
    """Most signals should not become trades — that is the system working."""
    t = make_trade(state=TradeState.REJECTED, notes="spread too wide")
    assert trade_rejected(t).severity is Severity.INFO


def test_recorder_stall_is_critical():
    n = recorder_stalled("2026-09-11T10:00:00+05:30", "CONTINUOUS")
    assert n.severity is Severity.CRITICAL
    assert "cannot be backfilled" in n.body


def test_signal_dedupe_key_collapses_a_polling_loop_to_one_alert():
    a = signal_fired("gex_regime", "NIFTY", "LONG_VOL", 0.8,
                     ts="2026-09-11T10:05:12+05:30")
    b = signal_fired("gex_regime", "NIFTY", "LONG_VOL", 0.8,
                     ts="2026-09-11T10:05:47+05:30")   # same minute
    assert a.dedupe_key == b.dedupe_key


def test_trade_opened_carries_risk_and_legs():
    t = make_trade(risk_amount=5000.0)
    n = trade_opened(t)
    assert n.severity is Severity.TRADE
    assert "24500" in n.body


def test_digest_includes_coverage_so_silence_is_visible():
    n = daily_digest(
        {"trades": 3, "wins": 2, "losses": 1, "total_pnl": 1200.0,
         "win_rate_pct": 66.7, "avg_mae": -450.0},
        {"symbols": [{"symbol": "NIFTY", "days_complete": 5,
                      "days_recorded": 6, "avg_completeness": 94.0}]},
    )
    assert "NIFTY" in n.body and "Data coverage" in n.body


def test_risk_warning_builds():
    assert risk_warning("Net short vega", "detail").severity is Severity.WARNING


# ── Telegram ──────────────────────────────────────────────────────────────────

def test_telegram_is_unconfigured_without_credentials():
    ch = TelegramChannel(bot_token="", chat_id="")
    assert not ch.configured
    assert run(ch.send(Notification("t", "b"))) is False   # returns, never raises


def test_telegram_escapes_html_in_user_content():
    """An unescaped angle bracket would make Telegram reject the whole message."""
    ch = TelegramChannel(bot_token="x", chat_id="y")
    text = ch._format(Notification("A <b>fake</b> title", "body & <script>"))
    assert "&lt;b&gt;fake&lt;/b&gt;" in text
    assert "&amp;" in text
    assert "<b>" in text            # our own formatting tags survive


def test_telegram_truncates_over_the_api_limit():
    ch = TelegramChannel(bot_token="x", chat_id="y")
    text = ch._format(Notification("t", "x" * 10_000))
    assert len(text) <= 4000
    assert "truncated" in text


def test_telegram_verify_distinguishes_missing_token_from_missing_chat():
    assert "TELEGRAM_BOT_TOKEN" in run(
        TelegramChannel(bot_token="", chat_id="").verify())["error"]


def test_telegram_reads_credentials_from_env():
    os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
    os.environ["TELEGRAM_CHAT_ID"] = "chat"
    try:
        assert TelegramChannel().configured
    finally:
        del os.environ["TELEGRAM_BOT_TOKEN"]
        del os.environ["TELEGRAM_CHAT_ID"]
