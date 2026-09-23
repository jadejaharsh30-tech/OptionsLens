# optionslens/backend/eod_vol.py
"""
Constant-maturity ATM implied volatility.

Front-expiry ATM IV is not a volatility series. It is a volatility series with
the expiry roll stamped on top of it, and on NIFTY weeklies the roll is the
larger of the two signals. Measured over 29 sessions of exchange EOD data, the
front-expiry series swung 6.80 vol points peak to trough while the 30-day
constant-maturity series built from the same chains swung 2.20. The extra 4.6
points were the contract getting closer to expiry, not the market repricing
risk.

This matters because IV Rank and any variance-risk-premium signal rank today's
reading against its own history. Rank a front-expiry series and you mostly
learn what day of the expiry cycle it is.

The fix is the one VIX uses: interpolate between the two expiries that bracket
a fixed tenor, in TOTAL VARIANCE rather than in vol, because variance is what
is additive in time.

Extrapolation is allowed only a few days past the nearest expiry. Monthly-only
underlyings (every single stock) need it: for the first days after a monthly
expiry the nearest listed contract is already 31-35 days out, so 30 days is not
bracketed and refusing outright would blank the series several days a month.
Further than that, extrapolated variance goes wrong fast in a steep term
structure, and a gap is the honest answer.

Pure functions over plain floats. No broker, no database, no config.
"""
from typing import Optional

# The tenor to standardise on. 30 calendar days matches the VIX convention,
# roughly matches RV 20d, and sits inside the bracket on every NIFTY and
# BANKNIFTY session measured, so their series never rely on extrapolation.
DEFAULT_TARGET_DAYS = 30.0

# How far past the nearest expiry the target may sit before we give up. Wide
# enough to cover the post-monthly-expiry gap on stocks, narrow enough that a
# weekly-only curve is never stretched to a month.
MAX_EXTRAPOLATION_DAYS = 5.0


def constant_maturity_iv(points: list[tuple[float, float]],
                         target_days: float = DEFAULT_TARGET_DAYS,
                         max_extrapolation_days: float = MAX_EXTRAPOLATION_DAYS,
                         ) -> Optional[float]:
    """
    Interpolate ATM IV to a fixed tenor from per-expiry ATM IV readings.

    `points` are (tenor_in_days, iv) pairs for one symbol on one date, where iv
    is a decimal (0.14, not 14.0). Order does not matter; duplicates at the same
    tenor are collapsed to their mean.

    Uses the nearest expiry on each side of the target. When the target is not
    bracketed, extrapolates along the two nearest expiries on the side that
    exists, but only if the target is within `max_extrapolation_days` of the
    nearer one. Otherwise returns None rather than a confident wrong number;
    callers already handle None from the IV solver for the same reason.

    An exact tenor match short-circuits, so a chain that happens to carry a
    30-day expiry is not pushed through the interpolation at all.
    """
    clean = _collapse(points)
    if not clean:
        return None

    for tenor, iv in clean:
        if abs(tenor - target_days) < 1e-9:
            return iv

    pair = _pick(clean, target_days)
    if pair is None:
        return None
    (t1, iv1), (t2, iv2) = pair

    bracketed = t1 < target_days < t2
    if not bracketed:
        gap = min(abs(t1 - target_days), abs(t2 - target_days))
        if gap > max_extrapolation_days:
            return None

    # Total variance is additive in time; linear interpolation in vol is not
    # arbitrage-free and visibly distorts a steep front end.
    w1 = iv1 * iv1 * t1
    w2 = iv2 * iv2 * t2
    w = w1 + (w2 - w1) * (target_days - t1) / (t2 - t1)
    if w <= 0:
        return None
    return (w / target_days) ** 0.5


def expiries_for_tenor(tenors: list[float],
                       target_days: float = DEFAULT_TARGET_DAYS) -> list[int]:
    """
    Indices into `tenors` of the expiries constant_maturity_iv would use.

    Lets a caller that pays per chain fetch (the live endpoint, the daily job)
    pull exactly the chains it needs rather than every listed expiry. Returns
    the nearest expiry either side of the target, or the two nearest on the one
    side that exists, or a single index on an exact match. Non-positive tenors
    are ignored. Empty when fewer than two usable expiries exist.
    """
    usable = [(t, i) for i, t in enumerate(tenors) if t is not None and t > 0]
    for t, i in usable:
        if abs(t - target_days) < 1e-9:
            return [i]
    pair = _pick(sorted(usable), target_days)
    if pair is None:
        return []
    return [pair[0][1], pair[1][1]]


def term_structure_slope(points: list[tuple[float, float]],
                         near_days: float = 30.0,
                         far_days: float = 60.0) -> Optional[float]:
    """
    Far minus near constant-maturity IV, in vol points.

    Negative is backwardation — the front is bid relative to the back, which is
    the classic stress signature. Returns None when either leg is unbracketed.
    """
    near = constant_maturity_iv(points, near_days)
    far = constant_maturity_iv(points, far_days)
    if near is None or far is None:
        return None
    return far - near


def percentile_rank(history: list[float], current: float) -> Optional[float]:
    """
    Where `current` sits in `history`, as a percentage.

    This is percentile rank, not IV Rank. IV Rank divides by the high-low range
    and so is dominated by a single outlier day; percentile rank counts
    observations and is not. Both are reported in the roadmap's own terms, but
    only this one survives a spike.

    Returns None below 20 observations. A percentile computed on a handful of
    points reads as precise and is not.
    """
    clean = [v for v in history if v is not None]
    if len(clean) < 20:
        return None
    below = sum(1 for v in clean if v < current)
    ties = sum(1 for v in clean if v == current)
    return (below + 0.5 * ties) / len(clean) * 100.0


def _pick(ordered: list[tuple[float, object]],
          target: float) -> Optional[tuple[tuple, tuple]]:
    """The two tenor-sorted entries the target should be read from, or None."""
    below = [p for p in ordered if p[0] < target]
    above = [p for p in ordered if p[0] > target]
    if below and above:
        return below[-1], above[0]
    if len(above) >= 2:
        return above[0], above[1]
    if len(below) >= 2:
        return below[-2], below[-1]
    return None


def _collapse(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop unusable readings, average duplicate tenors, sort by tenor."""
    grouped: dict[float, list[float]] = {}
    for tenor, iv in points:
        if tenor is None or iv is None:
            continue
        if tenor <= 0 or iv <= 0:
            continue
        grouped.setdefault(float(tenor), []).append(float(iv))
    return sorted((t, sum(v) / len(v)) for t, v in grouped.items())
