# optionslens/backend/forward_engine.py
"""
Implied forward extraction from an option chain.

Options are priced off the forward, but Fyers gives us spot and an option chain,
not a forward. Rather than guess a dividend yield, we recover the forward the
market is actually using, straight out of put-call parity:

    C - P = e^(-rT) (F - K)      =>      F = K + e^(rT) (C - P)

In theory every strike returns the same F. In practice wide strikes are noisy,
so we follow the CBOE VIX methodology: pick the strike where |C - P| is
smallest — the strike closest to the true forward, and typically the most
liquid — and solve there. A median across near-ATM strikes is available as a
more robust variant.

This module is pure math over (strike, call_price, put_price) tuples. It knows
nothing about Fyers or FastAPI, so it is unit-testable and reusable by the
backtester.
"""
import math
from typing import Iterable, Optional

# A forward this far from spot means the chain is stale or crossed, not that
# the market has a 25% dividend yield. Reject rather than poison every IV.
MAX_FORWARD_DEVIATION = 0.25


def forward_from_strike(strike: float, call_price: float, put_price: float,
                        T: float, r: float) -> float:
    """Forward implied by put-call parity at a single strike."""
    return strike + math.exp(r * T) * (call_price - put_price)


def implied_forward(
    pairs: Iterable[tuple[float, Optional[float], Optional[float]]],
    T: float,
    r: float,
    spot: Optional[float] = None,
) -> Optional[float]:
    """
    Recover the forward from a chain using the VIX-style minimum-|C-P| rule.

    Args:
        pairs: (strike, call_price, put_price) triples. Prices should be bid-ask
               mids where available — LTP goes stale on illiquid strikes and a
               stale leg biases the forward directly.
        T: time to expiry in years
        r: risk-free rate
        spot: optional sanity reference; a wildly off forward is rejected

    Returns:
        Implied forward, or None when no strike has two usable quotes.
    """
    if T <= 0:
        return None

    usable = [
        (k, c, p) for k, c, p in pairs
        if k > 0 and c is not None and p is not None and c > 0 and p > 0
    ]
    if not usable:
        return None

    strike, call_price, put_price = min(usable, key=lambda x: abs(x[1] - x[2]))
    forward = forward_from_strike(strike, call_price, put_price, T, r)

    if forward <= 0:
        return None
    if spot and spot > 0 and abs(forward / spot - 1.0) > MAX_FORWARD_DEVIATION:
        return None
    return forward


def implied_forward_robust(
    pairs: Iterable[tuple[float, Optional[float], Optional[float]]],
    T: float,
    r: float,
    spot: float,
    moneyness_band: float = 0.03,
) -> Optional[float]:
    """
    Median forward across strikes within `moneyness_band` of spot.

    Less sensitive to one stale quote than the single-strike rule, at the cost
    of mixing in slightly less liquid strikes. Falls back to `implied_forward`
    when the band is empty.
    """
    if T <= 0 or spot <= 0:
        return None

    near = [
        (k, c, p) for k, c, p in pairs
        if k > 0 and c is not None and p is not None and c > 0 and p > 0
        and abs(k / spot - 1.0) <= moneyness_band
    ]
    if not near:
        return implied_forward(pairs, T, r, spot)

    forwards = sorted(forward_from_strike(k, c, p, T, r) for k, c, p in near)
    mid = len(forwards) // 2
    forward = (forwards[mid] if len(forwards) % 2
               else (forwards[mid - 1] + forwards[mid]) / 2.0)

    if forward <= 0 or abs(forward / spot - 1.0) > MAX_FORWARD_DEVIATION:
        return None
    return forward


def implied_dividend_yield(forward: float, spot: float,
                           T: float, r: float) -> Optional[float]:
    """
    Back out the continuous dividend yield the forward implies:
        F = S e^((r - q) T)  =>  q = r - ln(F/S) / T

    Diagnostic rather than an input — a q that drifts far from the index's known
    yield is a signal that the chain is stale or the forward fit is bad.
    """
    if T <= 0 or spot <= 0 or forward <= 0:
        return None
    return r - math.log(forward / spot) / T


def forward_basis_pct(forward: float, spot: float) -> Optional[float]:
    """Forward premium/discount over spot, in percent. Negative = backwardation."""
    if spot <= 0 or forward <= 0:
        return None
    return (forward / spot - 1.0) * 100.0
