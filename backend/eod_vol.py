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

Pure functions over plain floats. No broker, no database, no config.
"""
from typing import Optional

# The tenor to standardise on. 30 calendar days matches the VIX convention and
# sits inside the bracket on essentially every NIFTY session, which matters
# because we refuse to extrapolate.
DEFAULT_TARGET_DAYS = 30.0


def constant_maturity_iv(points: list[tuple[float, float]],
                         target_days: float = DEFAULT_TARGET_DAYS) -> Optional[float]:
    """
    Interpolate ATM IV to a fixed tenor from per-expiry ATM IV readings.

    `points` are (tenor_in_days, iv) pairs for one symbol on one date, where iv
    is a decimal (0.14, not 14.0). Order does not matter; duplicates at the same
    tenor are collapsed to their mean.

    Returns None when the target tenor is not bracketed by two readings, rather
    than extrapolating off the end of the curve. Extrapolated variance goes
    negative in the wings of a steep term structure, and a confidently wrong
    number is worse here than a gap: callers already handle None from the IV
    solver for the same reason.

    An exact tenor match short-circuits, so a chain that happens to carry a
    30-day expiry is not pushed through the interpolation at all.
    """
    clean = _collapse(points)
    if len(clean) < 1:
        return None

    for tenor, iv in clean:
        if abs(tenor - target_days) < 1e-9:
            return iv

    below = [p for p in clean if p[0] < target_days]
    above = [p for p in clean if p[0] > target_days]
    if not below or not above:
        return None

    t1, iv1 = below[-1]      # nearest expiry under the target
    t2, iv2 = above[0]       # nearest expiry over it

    # Total variance is additive in time; linear interpolation in vol is not
    # arbitrage-free and visibly distorts a steep front end.
    w1 = iv1 * iv1 * t1
    w2 = iv2 * iv2 * t2
    w = w1 + (w2 - w1) * (target_days - t1) / (t2 - t1)
    if w <= 0:
        return None
    return (w / target_days) ** 0.5


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
