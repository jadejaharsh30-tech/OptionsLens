# optionslens/backend/trading/entry.py
"""
Entry gating.

A signal firing is not a reason to trade. Between the signal and the fill sit
questions the signal cannot answer: is this strike actually tradeable, has the
setup already gone, is there budget left. Each check below has cost me nothing
to write and would cost real money to omit.

The revalidation check is the subtle one. Signals are evaluated on a snapshot;
fills happen later. If price has already moved to where the signal would no
longer fire, entering is chasing — you get the worse price and the thinner edge.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class EntryRules:
    """Gates a proposal must clear before it becomes an order."""
    max_spread_pct:        float = 3.0    # bid-ask as % of mid
    min_oi:                float = 500.0
    min_volume:            float = 100.0
    max_price_drift_pct:   float = 0.30   # underlying drift since signal
    require_revalidation:  bool  = True
    use_limit_orders:      bool  = True
    limit_offset_ticks:    float = 1.0    # how far through mid we will pay
    min_premium:           float = 1.0    # sub-rupee options are noise
    max_premium_pct_spot:  float = 5.0


@dataclass
class EntryCheck:
    passed: bool
    reasons: list[str] = field(default_factory=list)

    def fail(self, reason: str):
        self.passed = False
        self.reasons.append(reason)


def check_entry(
    bid: float,
    ask: float,
    ltp: float,
    oi: float,
    volume: float,
    spot_now: float,
    spot_at_signal: float,
    rules: EntryRules,
    signal_still_valid: Optional[bool] = None,
) -> EntryCheck:
    """
    Decide whether a proposal is still worth filling.

    Collects every failure rather than short-circuiting, so a rejected trade
    records the full picture — "spread 8% and OI 120" is diagnosable in a way
    that "spread too wide" alone is not.
    """
    check = EntryCheck(passed=True)

    # A one-sided book is not a market. Nothing else matters if we cannot price.
    if bid <= 0 or ask <= 0:
        check.fail("no two-sided quote — cannot establish a fair fill")
        return check

    mid = (bid + ask) / 2.0
    spread_pct = (ask - bid) / mid * 100.0

    if spread_pct > rules.max_spread_pct:
        check.fail(
            f"spread {spread_pct:.2f}% exceeds {rules.max_spread_pct}% — "
            f"crossing it would cost more than most signals are worth"
        )

    if mid < rules.min_premium:
        check.fail(f"premium {mid:.2f} below {rules.min_premium} — tick noise")

    if spot_now > 0 and mid > spot_now * rules.max_premium_pct_spot / 100.0:
        check.fail(f"premium {mid:.2f} is an implausible share of spot")

    if oi < rules.min_oi:
        check.fail(f"OI {oi:.0f} below {rules.min_oi} — likely hard to exit")

    if volume < rules.min_volume:
        check.fail(f"volume {volume:.0f} below {rules.min_volume} — not trading today")

    if spot_at_signal > 0:
        drift = abs(spot_now / spot_at_signal - 1.0) * 100.0
        if drift > rules.max_price_drift_pct:
            check.fail(
                f"underlying moved {drift:.2f}% since the signal — "
                f"entering now is chasing, not following"
            )

    if rules.require_revalidation and signal_still_valid is False:
        check.fail("signal no longer valid at fill time")

    return check


def limit_price(bid: float, ask: float, is_buy: bool,
                rules: EntryRules, tick_size: float = 0.05) -> float:
    """
    Where to place a limit order.

    Sits `limit_offset_ticks` through the mid toward the side we need, rather
    than at the touch. Patient enough to save most of the spread, aggressive
    enough to actually fill.
    """
    mid = (bid + ask) / 2.0
    offset = rules.limit_offset_ticks * tick_size
    price = mid + offset if is_buy else mid - offset
    # Never post through the opposite side of the book.
    price = min(price, ask) if is_buy else max(price, bid)
    return round(max(price, tick_size) / tick_size) * tick_size
