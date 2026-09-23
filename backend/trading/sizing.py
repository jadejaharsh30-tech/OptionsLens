# optionslens/backend/trading/sizing.py
"""
Position sizing against a risk budget.

Two things make options sizing different from equity sizing, and both are
handled explicitly here:

1. **You cannot buy a fraction of a lot.** NIFTY trades in lots of 75. If your
   risk budget allows 0.8 lots, the honest answer is zero lots, not one. Naive
   sizing rounds up and silently takes ~25% more risk than the budget allowed,
   every time.

2. **Long and short options have different risk shapes.** A long option's max
   loss is the premium — bounded and knowable at entry. A short option's loss is
   unbounded, so sizing off premium received is nonsense: collecting 50 does not
   mean risking 50. Shorts are sized off the *stop distance* instead, and the
   result is labelled as stop-dependent, because the risk is only real if the
   stop is actually honoured.
"""
import math
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RiskConfig:
    """Risk limits. All percentages are of `capital`."""
    capital:                  float = 500_000.0
    risk_per_trade_pct:       float = 1.0
    daily_risk_budget_pct:    float = 3.0
    max_concurrent_positions: int   = 5
    max_lots_per_trade:       int   = 10
    # Assumed stop for short legs, as a multiple of premium received. A 2.0
    # means "we will cut at twice the credit", which is what makes the loss
    # bounded enough to size against at all.
    short_stop_multiple:      float = 2.0

    @property
    def risk_per_trade(self) -> float:
        return self.capital * self.risk_per_trade_pct / 100.0

    @property
    def daily_risk_budget(self) -> float:
        return self.capital * self.daily_risk_budget_pct / 100.0


@dataclass
class SizingResult:
    lots:            int
    risk_amount:     float          # INR genuinely at risk if the stop holds
    max_loss:        float
    risk_per_lot:    float
    reason:          str
    stop_dependent:  bool = False   # True when risk relies on honouring a stop

    @property
    def tradeable(self) -> bool:
        return self.lots > 0


def risk_per_lot(premium: float, lot_size: int, is_buy: bool,
                 stop_loss_pct: float, cfg: RiskConfig) -> float:
    """
    INR at risk for one lot.

    Long:  premium x lot_size x stop%       (bounded by the premium itself)
    Short: premium x lot_size x (multiple-1) (only bounded if the stop is kept)
    """
    notional = premium * lot_size
    if is_buy:
        # Never claim to risk more than the premium — a long option cannot lose
        # more than it cost, whatever the stop says.
        return notional * min(stop_loss_pct / 100.0, 1.0)
    return notional * max(cfg.short_stop_multiple - 1.0, 0.1)


def size_position(
    premium: float,
    lot_size: int,
    is_buy: bool,
    cfg: RiskConfig,
    stop_loss_pct: float = 40.0,
    risk_already_committed: float = 0.0,
    open_positions: int = 0,
) -> SizingResult:
    """
    Size one leg against the risk budget.

    Returns zero lots with an explanatory reason rather than raising — "budget
    does not support a single lot" is a normal outcome, and the reason belongs
    in the trade record so a skipped signal can be audited later.
    """
    if premium <= 0 or lot_size <= 0:
        return SizingResult(0, 0.0, 0.0, 0.0, "invalid premium or lot size")

    if open_positions >= cfg.max_concurrent_positions:
        return SizingResult(
            0, 0.0, 0.0, 0.0,
            f"at position limit ({open_positions}/{cfg.max_concurrent_positions})",
        )

    per_lot = risk_per_lot(premium, lot_size, is_buy, stop_loss_pct, cfg)
    if per_lot <= 0:
        return SizingResult(0, 0.0, 0.0, 0.0, "risk per lot resolved to zero")

    # The tighter of per-trade risk and what remains of the daily budget.
    remaining_daily = cfg.daily_risk_budget - risk_already_committed
    if remaining_daily <= 0:
        return SizingResult(
            0, 0.0, 0.0, per_lot,
            f"daily risk budget exhausted "
            f"({risk_already_committed:.0f}/{cfg.daily_risk_budget:.0f})",
        )

    allowed = min(cfg.risk_per_trade, remaining_daily)

    # Floor, never round. Rounding up quietly exceeds the budget.
    lots = int(math.floor(allowed / per_lot))
    lots = min(lots, cfg.max_lots_per_trade)

    if lots < 1:
        return SizingResult(
            0, 0.0, 0.0, per_lot,
            f"budget {allowed:.0f} below one lot's risk {per_lot:.0f} — "
            f"cannot trade a fraction of a lot",
        )

    risk_amount = lots * per_lot
    max_loss = (premium * lot_size * lots) if is_buy else risk_amount

    return SizingResult(
        lots            = lots,
        risk_amount     = round(risk_amount, 2),
        max_loss        = round(max_loss, 2),
        risk_per_lot    = round(per_lot, 2),
        reason          = f"{lots} lot(s) at {per_lot:.0f} risk each",
        stop_dependent  = not is_buy,
    )


def committed_risk(open_trades: list) -> float:
    """Total risk currently live, for the daily-budget check."""
    return sum(t.risk_amount or 0.0 for t in open_trades)
