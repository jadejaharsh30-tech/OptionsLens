# optionslens/backend/signals/features.py
"""
Derived features computed from a raw ChainSnapshot.

This is the "derive late" half of the recorder's "record raw, derive late"
rule. Nothing here is persisted by the recorder — it is all recomputed on read,
so improving the pricing model retroactively improves every historical day
instead of invalidating it.

Every function tolerates missing data and returns None rather than a fabricated
default. A signal reading `None` knows it cannot act; a signal reading a
silently-defaulted zero does not.
"""
import math
from dataclasses import dataclass, field
from typing import Optional

from chain_pricing import implied_forward_for_chain, price_for_iv
from config import RISK_FREE_RATE
from iv_engine import black76_greeks, implied_vol_forward
from market_hours import time_to_expiry
from recorder.models import ChainSnapshot


@dataclass
class StrikeFeatures:
    """Per-strike derived values for one side of the chain."""
    strike:      float
    option_type: str
    iv:          Optional[float] = None      # decimal, e.g. 0.15
    delta:       Optional[float] = None
    gamma:       Optional[float] = None
    vega:        Optional[float] = None
    theta:       Optional[float] = None
    oi:          float = 0.0
    volume:      float = 0.0
    price:       Optional[float] = None
    spread_pct:  Optional[float] = None


@dataclass
class ChainFeatures:
    """
    Everything derivable from a single snapshot.

    Deliberately snapshot-scoped: anything needing history (percentile ranks,
    OI velocity, realized vol) belongs in the signal, which has the window.
    """
    ts:           str
    symbol:       str
    spot:         float
    forward:      Optional[float] = None
    T:            float = 0.0
    atm_strike:   Optional[float] = None
    atm_iv:       Optional[float] = None
    strikes:      dict[tuple[float, str], StrikeFeatures] = field(default_factory=dict)

    # Structure
    net_gex:      Optional[float] = None
    gamma_flip:   Optional[float] = None
    pcr_oi:       float = 0.0
    max_pain:     Optional[float] = None
    rr_25d:       Optional[float] = None     # 25-delta risk reversal (vol points)

    def iv_at(self, strike: float, option_type: str) -> Optional[float]:
        f = self.strikes.get((strike, option_type))
        return f.iv if f else None


def compute_features(snapshot: ChainSnapshot,
                     lot_size: int = 1) -> ChainFeatures:
    """
    Derive the full feature set for one snapshot.

    `lot_size` scales GEX into contract terms; pass the symbol's real lot size
    from config when the absolute magnitude matters, or leave it at 1 when only
    the sign and the flip level matter (which is usually the case).
    """
    T = time_to_expiry(snapshot.expiry_date)
    rows = [
        {"strike": r.strike, "option_type": r.option_type, "ltp": r.ltp,
         "bid": r.bid, "ask": r.ask}
        for r in snapshot.rows
    ]
    forward = implied_forward_for_chain(rows, T, snapshot.spot) if T > 0 else None

    feats = ChainFeatures(
        ts         = snapshot.ts,
        symbol     = snapshot.symbol,
        spot       = snapshot.spot,
        forward    = forward,
        T          = T,
        atm_strike = snapshot.atm_strike(),
        pcr_oi     = snapshot.pcr() or 0.0,
        max_pain   = compute_max_pain(snapshot),
    )

    if forward is None or T <= 0:
        # No forward means no trustworthy IV. Return structural features only
        # rather than falling back to spot-based pricing, which would quietly
        # mix two different models into one series.
        return feats

    for row in snapshot.rows:
        price = price_for_iv({"ltp": row.ltp, "bid": row.bid, "ask": row.ask})
        iv = None
        g: dict = {}
        if price:
            iv = implied_vol_forward(price, forward, row.strike, T,
                                     RISK_FREE_RATE, row.option_type)
            if iv is not None:
                g = black76_greeks(forward, row.strike, T, RISK_FREE_RATE,
                                   iv, row.option_type, spot=snapshot.spot)

        feats.strikes[(row.strike, row.option_type)] = StrikeFeatures(
            strike      = row.strike,
            option_type = row.option_type,
            iv          = iv,
            delta       = g.get("delta"),
            gamma       = g.get("gamma"),
            vega        = g.get("vega"),
            theta       = g.get("theta"),
            oi          = row.oi,
            volume      = row.volume,
            price       = price,
            spread_pct  = row.spread_pct,
        )

    feats.atm_iv     = _atm_iv(feats)
    feats.net_gex    = _net_gex(feats, lot_size)
    feats.gamma_flip = _gamma_flip_level(feats, lot_size)
    feats.rr_25d     = _risk_reversal_25d(feats)
    return feats


# ── Individual features ───────────────────────────────────────────────────────

def _atm_iv(feats: ChainFeatures) -> Optional[float]:
    """Average of call and put IV at the ATM strike."""
    if feats.atm_strike is None:
        return None
    ivs = [
        f.iv for f in (
            feats.strikes.get((feats.atm_strike, "CE")),
            feats.strikes.get((feats.atm_strike, "PE")),
        ) if f and f.iv is not None
    ]
    return sum(ivs) / len(ivs) if ivs else None


def _net_gex(feats: ChainFeatures, lot_size: int) -> Optional[float]:
    """
    Net dealer gamma exposure across the chain.

    Sign convention matches gex_engine: dealers are assumed short options, so
    call gamma contributes positively and put gamma negatively. Positive net
    GEX implies dealers are long gamma and hedging dampens moves.
    """
    total, seen = 0.0, False
    for f in feats.strikes.values():
        if f.gamma is None:
            continue
        seen = True
        contrib = f.gamma * f.oi * lot_size * feats.spot ** 2 * 0.01
        total += contrib if f.option_type == "CE" else -contrib
    return total if seen else None


def gex_by_strike(feats: ChainFeatures, lot_size: int = 1) -> dict[float, float]:
    """Net GEX per strike — the profile the flip level is found from."""
    profile: dict[float, float] = {}
    for f in feats.strikes.values():
        if f.gamma is None:
            continue
        contrib = f.gamma * f.oi * lot_size * feats.spot ** 2 * 0.01
        profile[f.strike] = profile.get(f.strike, 0.0) + (
            contrib if f.option_type == "CE" else -contrib
        )
    return profile


def _gamma_flip_level(feats: ChainFeatures, lot_size: int) -> Optional[float]:
    """
    The spot level where cumulative net GEX crosses zero.

    This, not the raw GEX number, is the tradeable quantity: above the flip
    dealers are long gamma and hedging is stabilising; below it they are short
    and hedging amplifies moves. Interpolated linearly between the bracketing
    strikes.
    """
    profile = gex_by_strike(feats, lot_size)
    if len(profile) < 2:
        return None

    strikes = sorted(profile)
    cumulative, running = [], 0.0
    for k in strikes:
        running += profile[k]
        cumulative.append(running)

    for i in range(len(cumulative) - 1):
        lo, hi = cumulative[i], cumulative[i + 1]
        if lo == 0:
            return strikes[i]
        if (lo < 0) != (hi < 0):
            span = hi - lo
            if span == 0:
                return strikes[i]
            frac = -lo / span
            return strikes[i] + frac * (strikes[i + 1] - strikes[i])
    return None


def compute_max_pain(snapshot: ChainSnapshot) -> Optional[float]:
    """Strike minimising total writer payout — where option sellers lose least."""
    strikes = snapshot.strikes()
    if not strikes:
        return None

    pain = {}
    for test in strikes:
        total = 0.0
        for r in snapshot.rows:
            if r.option_type == "CE":
                total += max(test - r.strike, 0) * r.oi
            else:
                total += max(r.strike - test, 0) * r.oi
        pain[test] = total
    return min(pain, key=pain.get)


def _risk_reversal_25d(feats: ChainFeatures) -> Optional[float]:
    """
    25-delta risk reversal: IV(25d call) - IV(25d put), in vol points.

    A standard positioning gauge — persistently negative means puts are bid
    relative to calls, i.e. the market is paying up for downside protection.
    Uses the strikes whose deltas sit closest to ±0.25.
    """
    calls = [f for f in feats.strikes.values()
             if f.option_type == "CE" and f.delta is not None and f.iv is not None]
    puts  = [f for f in feats.strikes.values()
             if f.option_type == "PE" and f.delta is not None and f.iv is not None]
    if not calls or not puts:
        return None

    call_25 = min(calls, key=lambda f: abs(f.delta - 0.25))
    put_25  = min(puts,  key=lambda f: abs(abs(f.delta) - 0.25))

    # Refuse to report a "25-delta" number built from a 5-delta wing.
    if abs(call_25.delta - 0.25) > 0.10 or abs(abs(put_25.delta) - 0.25) > 0.10:
        return None
    return (call_25.iv - put_25.iv) * 100.0


# ── History-dependent helpers (used by signals, which hold the window) ────────

def percentile_rank(value: float, history: list[float]) -> Optional[float]:
    """
    Where `value` sits within `history`, as 0-100.

    Returns None below 20 observations. A percentile computed from five points
    is not a percentile, and reporting one invites exactly the false confidence
    this framework exists to avoid.
    """
    clean = [h for h in history if h is not None]
    if len(clean) < 20:
        return None
    below = sum(1 for h in clean if h < value)
    return round(below / len(clean) * 100.0, 2)


def realized_vol_from_spots(spots: list[float], periods_per_year: float) -> Optional[float]:
    """
    Close-to-close realized vol, annualised by `periods_per_year`.

    For minute bars during a 385-minute session that is 385 * 252.
    """
    clean = [s for s in spots if s and s > 0]
    if len(clean) < 3:
        return None
    rets = [math.log(b / a) for a, b in zip(clean, clean[1:])]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    return math.sqrt(var * periods_per_year)
