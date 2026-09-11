# optionslens/backend/notify/events.py
"""
Notification builders for domain events.

Formatting lives here rather than in the trade manager so that what a message
says can change without touching position management, and so every alert for a
given event type reads the same way.

Content principle: a phone alert should carry enough to decide without opening
the laptop. "Stop hit" is not actionable; "stop hit, -2,340, exit was 38% below
entry, MAE was -2,900" tells you whether to look closer or let it go.
"""
from typing import Optional

from notify.base import Notification, Severity
from trading.models import Trade


def _legs(trade: Trade) -> str:
    return "\n".join(f"  {leg.describe()}" for leg in trade.legs)


def signal_fired(signal_id: str, symbol: str, direction: str, strength: float,
                 detail: str = "", ts: str = "") -> Notification:
    return Notification(
        title=f"{symbol} · {signal_id}",
        body=f"{direction} (strength {strength:.2f})\n{detail}".strip(),
        severity=Severity.SIGNAL,
        # One alert per signal per symbol per minute, however often we poll.
        dedupe_key=f"signal:{signal_id}:{symbol}:{ts[:16]}",
        meta={"ts": ts} if ts else {},
    )


def trade_opened(trade: Trade) -> Notification:
    entry = trade.entry_cost()
    return Notification(
        title=f"Opened · {trade.symbol}",
        body=(
            f"{_legs(trade)}\n\n"
            f"Net premium: {entry:+,.2f}\n"
            f"Risk: {trade.risk_amount:,.2f}"
            if trade.risk_amount else _legs(trade)
        ),
        severity=Severity.TRADE,
        dedupe_key=f"open:{trade.trade_id}",
        meta={
            "trade": trade.trade_id,
            "signal": trade.signal_id or "manual",
            "paper": trade.paper,
        },
    )


def trade_closed(trade: Trade) -> Notification:
    pnl = trade.realized_pnl or 0.0
    entry = abs(trade.entry_cost())
    pct = (pnl / entry * 100.0) if entry else 0.0

    # A stop or a risk breach should look different on a phone from a target.
    severity = (Severity.CRITICAL
                if trade.exit_reason and trade.exit_reason.value == "STOP_LOSS"
                else Severity.TRADE)

    body = [
        f"{trade.symbol} · {trade.exit_reason.value if trade.exit_reason else 'CLOSED'}",
        f"P&L: {pnl:+,.2f} ({pct:+.1f}%)",
        f"Charges: {trade.total_charges:,.2f}",
    ]
    if trade.mae is not None and trade.mfe is not None:
        body.append(f"MAE {trade.mae:+,.0f} · MFE {trade.mfe:+,.0f}")
    if trade.notes:
        body.append(f"\n{trade.notes}")

    return Notification(
        title=f"{'Loss' if pnl < 0 else 'Profit'} · {trade.symbol}",
        body="\n".join(body),
        severity=severity,
        dedupe_key=f"close:{trade.trade_id}",
        meta={"trade": trade.trade_id},
    )


def trade_rejected(trade: Trade) -> Notification:
    """
    Sent at INFO, not WARNING.

    Rejections are the system working — most signals should not become trades.
    Alerting on them at warning level would make the important messages harder
    to see, which is the opposite of the point.
    """
    return Notification(
        title=f"Signal not taken · {trade.symbol}",
        body=f"{trade.notes or 'rejected'}\n\n{_legs(trade)}",
        severity=Severity.INFO,
        dedupe_key=f"reject:{trade.trade_id}",
    )


def risk_warning(message: str, detail: str = "") -> Notification:
    return Notification(
        title="Portfolio risk",
        body=f"{message}\n{detail}".strip(),
        severity=Severity.WARNING,
        dedupe_key=f"risk:{message[:40]}",
    )


def recorder_stalled(last_write: Optional[str], phase: str) -> Notification:
    """The alert that protects the irreplaceable asset."""
    return Notification(
        title="Recorder not writing",
        body=(
            f"Session phase is {phase} but no snapshot has been written"
            f"{f' since {last_write}' if last_write else ' at all today'}.\n\n"
            f"Intraday chain data cannot be backfilled — every minute lost is "
            f"lost permanently."
        ),
        severity=Severity.CRITICAL,
        dedupe_key="recorder_stalled",
    )


def daily_digest(stats: dict, coverage: dict) -> Notification:
    """End-of-day summary: what fired, what traded, and whether data is healthy."""
    lines = [
        f"Trades: {stats.get('trades', 0)} "
        f"({stats.get('wins', 0)}W / {stats.get('losses', 0)}L)",
    ]
    if stats.get("total_pnl") is not None:
        lines.append(f"P&L: {stats['total_pnl']:+,.2f}")
    if stats.get("win_rate_pct") is not None:
        lines.append(f"Win rate: {stats['win_rate_pct']:.1f}%")
    if stats.get("avg_mae") is not None:
        lines.append(f"Avg MAE: {stats['avg_mae']:+,.0f}")

    syms = coverage.get("symbols", [])
    if syms:
        lines.append("\nData coverage:")
        for s in syms:
            lines.append(
                f"  {s['symbol']}: {s['days_complete']}/{s['days_recorded']} "
                f"complete ({s['avg_completeness']:.0f}%)"
            )

    return Notification(
        title="Daily digest",
        body="\n".join(lines),
        severity=Severity.INFO,
        dedupe_key="digest",
    )
