# optionslens/backend/chain_pricing.py
"""
Shared glue between a normalised Fyers option chain and the pricing engines.

Lives outside `routers/` so that chain, surface, oi and ivrank can all share one
definition of "what price do we solve IV against" and "what forward does this
chain imply". Previously each router answered those questions slightly
differently, so the same strike could report different IVs depending on which
endpoint you asked.

Input rows are the dicts produced by `fyers_client.fetch_option_chain`:
    {strike, option_type, oi, oi_change, oi_change_pct, prev_oi, ltp, bid, ask, volume}
No Fyers or FastAPI imports here — the backtester feeds it recorded rows.
"""
from typing import Optional

from forward_engine import implied_forward


def price_for_iv(row: dict) -> Optional[float]:
    """
    The price to solve implied volatility against.

    Bid-ask mid is preferred: LTP can be minutes stale on an illiquid strike,
    and a stale print shows up as a phantom IV spike in the surface. Falls back
    to LTP when the book is one-sided or empty.
    """
    bid = row.get("bid") or 0
    ask = row.get("ask") or 0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0

    ltp = row.get("ltp") or 0
    return ltp if ltp > 0 else None


def parity_pairs(chain: list[dict]) -> list[tuple[float, Optional[float], Optional[float]]]:
    """Collapse a flat chain into (strike, call_price, put_price) triples."""
    calls: dict[float, Optional[float]] = {}
    puts:  dict[float, Optional[float]] = {}

    for row in chain:
        target = calls if row.get("option_type") == "CE" else puts
        target[row["strike"]] = price_for_iv(row)

    return [(k, calls.get(k), puts.get(k)) for k in sorted(set(calls) | set(puts))]


def implied_forward_for_chain(chain: list[dict], T: float,
                              spot: float) -> Optional[float]:
    """
    Forward implied by this chain's own put-call parity.

    Returns None when no strike carries two usable quotes, in which case callers
    should skip IV rather than silently falling back to spot — a spot-based IV
    mixed into a forward-based surface is worse than a missing point.
    """
    return implied_forward(parity_pairs(chain), T, r=_rate(), spot=spot)


def _rate() -> float:
    """Imported lazily so this module stays free of config import cycles."""
    from config import RISK_FREE_RATE
    return RISK_FREE_RATE
