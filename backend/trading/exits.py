# optionslens/backend/trading/exits.py
"""
Exit rules for options positions.

A price stop alone is not risk management for an option. Three things can take
a position apart while price does exactly what you predicted:

- **Theta.** You were right, slowly, and decay ate the move.
- **Vol.** Direction was right, IV collapsed, the long lost anyway.
- **Drift.** The 0.3-delta call you bought is now 0.8 delta. Whatever thesis you
  had, you are no longer expressing it — you are long the underlying.

Each gets its own rule. The expiry flatten is non-negotiable: gamma and
assignment risk near expiry are not worth the last few rupees of premium.

Rules are evaluated in severity order and the first match wins, so a position
that is simultaneously past its stop and near expiry reports the stop.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from market_hours import IST, time_to_expiry
from trading.models import ExitReason, Trade


@dataclass(frozen=True)
class ExitRules:
    """Per-trade exit configuration."""
    stop_loss_pct:      float = 40.0          # of net entry premium
    target_pct:         float = 80.0
    max_holding_minutes: Optional[int] = None
    # Exit a long when IV has fallen this many % (relative) from entry IV.
    iv_crush_exit_pct:  Optional[float] = 25.0
    # Exit when net position delta has moved this far from its entry value.
    delta_drift_band:   Optional[float] = 0.35
    # Hard close this many minutes before expiry, regardless of P&L.
    flatten_before_expiry_minutes: int = 30
    trail_after_pct:    Optional[float] = None   # start trailing past this gain
    trail_giveback_pct: float = 30.0             # give back this much of peak


@dataclass
class ExitDecision:
    should_exit: bool
    reason:      Optional[ExitReason] = None
    detail:      str = ""

    @classmethod
    def hold(cls) -> "ExitDecision":
        return cls(should_exit=False)

    @classmethod
    def exit(cls, reason: ExitReason, detail: str) -> "ExitDecision":
        return cls(should_exit=True, reason=reason, detail=detail)


def evaluate_exits(
    trade: Trade,
    unrealized_pnl: float,
    rules: ExitRules,
    now: Optional[datetime] = None,
    current_iv: Optional[float] = None,
    current_delta: Optional[float] = None,
) -> ExitDecision:
    """
    Decide whether to close `trade` now.

    Args:
        unrealized_pnl: current MTM in INR, net of charges
        current_iv: position-level IV proxy (e.g. ATM IV of the traded expiry)
        current_delta: current net position delta
    """
    now = now or datetime.now(IST)

    entry_cost = abs(trade.entry_cost())
    if entry_cost <= 0:
        return ExitDecision.hold()

    pnl_pct = unrealized_pnl / entry_cost * 100.0

    # ── 1. Hard expiry flatten — outranks everything ─────────────────────────
    # Checked first because "we were about to hit target" is not a reason to
    # carry gamma into settlement.
    expiry_date = trade.legs[0].expiry_date if trade.legs else None
    if expiry_date:
        T_years = time_to_expiry(expiry_date, now)
        minutes_left = T_years * 365 * 24 * 60
        if minutes_left <= rules.flatten_before_expiry_minutes:
            return ExitDecision.exit(
                ExitReason.EXPIRY_FLATTEN,
                f"{minutes_left:.0f} min to expiry — flattening rather than "
                f"carrying gamma and assignment risk",
            )

    # ── 2. Stop loss ─────────────────────────────────────────────────────────
    if pnl_pct <= -rules.stop_loss_pct:
        return ExitDecision.exit(
            ExitReason.STOP_LOSS,
            f"P&L {pnl_pct:.1f}% past stop of -{rules.stop_loss_pct}%",
        )

    # ── 3. Target ────────────────────────────────────────────────────────────
    if pnl_pct >= rules.target_pct:
        return ExitDecision.exit(
            ExitReason.TARGET, f"P&L {pnl_pct:.1f}% reached target",
        )

    # ── 4. Trailing giveback ─────────────────────────────────────────────────
    if rules.trail_after_pct is not None and trade.mfe is not None:
        peak_pct = trade.mfe / entry_cost * 100.0
        if peak_pct >= rules.trail_after_pct:
            giveback = peak_pct - pnl_pct
            if giveback >= rules.trail_giveback_pct:
                return ExitDecision.exit(
                    ExitReason.TARGET,
                    f"gave back {giveback:.1f}% from a {peak_pct:.1f}% peak",
                )

    # ── 5. IV crush (longs only) ─────────────────────────────────────────────
    # A short benefits from the same move, so this must not fire on credits.
    if (rules.iv_crush_exit_pct is not None and current_iv is not None
            and trade.entry_cost() > 0):
        entry_ivs = [leg.entry_iv for leg in trade.legs if leg.entry_iv]
        if entry_ivs:
            entry_iv = sum(entry_ivs) / len(entry_ivs)
            if entry_iv > 0:
                drop_pct = (1 - current_iv / entry_iv) * 100.0
                if drop_pct >= rules.iv_crush_exit_pct:
                    return ExitDecision.exit(
                        ExitReason.IV_CRUSH,
                        f"IV fell {drop_pct:.1f}% from entry "
                        f"({entry_iv * 100:.1f}% to {current_iv * 100:.1f}%) — "
                        f"long premium is bleeding regardless of direction",
                    )

    # ── 6. Delta drift ───────────────────────────────────────────────────────
    if rules.delta_drift_band is not None and current_delta is not None:
        entry_deltas = [
            leg.entry_delta * leg.sign * leg.lots
            for leg in trade.legs if leg.entry_delta is not None
        ]
        if entry_deltas:
            entry_delta = sum(entry_deltas)
            drift = abs(current_delta - entry_delta)
            if drift >= rules.delta_drift_band:
                return ExitDecision.exit(
                    ExitReason.DELTA_DRIFT,
                    f"net delta moved {entry_delta:+.2f} to {current_delta:+.2f} — "
                    f"no longer the position that was put on",
                )

    # ── 7. Time stop ─────────────────────────────────────────────────────────
    if rules.max_holding_minutes is not None and trade.opened_at:
        opened = datetime.fromisoformat(trade.opened_at)
        held = (now - opened).total_seconds() / 60.0
        if held >= rules.max_holding_minutes:
            return ExitDecision.exit(
                ExitReason.TIME_STOP,
                f"held {held:.0f} min, limit {rules.max_holding_minutes} — "
                f"thesis has had its window",
            )

    return ExitDecision.hold()
