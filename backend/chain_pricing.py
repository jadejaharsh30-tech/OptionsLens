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
from iv_engine import implied_vol_forward


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


def atm_iv_for_chain(chain: list[dict], T: float,
                     spot: float) -> Optional[float]:
    """
    ATM implied vol for one expiry: the mean of the solvable call and put IVs at
    the strike nearest the chain's implied forward.

    This is the single definition used by the live IV Rank endpoint, the daily
    snapshot job and the bhavcopy importer. IV Rank compares today's reading
    against stored history, so any difference in how the two are computed shows
    up as a fake regime shift at the boundary.

    It used to be an average over every strike within 2% of spot. That band is
    asymmetric in a skewed smile, and when a volume gate drops some strikes (as
    the exchange-EOD history must) the band's composition changes day to day.
    One strike at the forward avoids both problems.

    Returns None when no forward can be implied or neither leg at the ATM strike
    solves — never a spot-based substitute.
    """
    forward = implied_forward_for_chain(chain, T, spot)
    if forward is None:
        return None

    priced = [row for row in chain if price_for_iv(row)]
    if not priced:
        return None
    atm = min({row["strike"] for row in priced}, key=lambda k: abs(k - forward))

    r = _rate()
    ivs = []
    for row in priced:
        if row["strike"] != atm:
            continue
        iv = implied_vol_forward(price_for_iv(row), forward, atm, T, r,
                                 row["option_type"])
        if iv is not None:
            ivs.append(iv)
    return sum(ivs) / len(ivs) if ivs else None


def _rate() -> float:
    """Imported lazily so this module stays free of config import cycles."""
    from config import RISK_FREE_RATE
    return RISK_FREE_RATE
