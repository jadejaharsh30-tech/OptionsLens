# optionslens/backend/backtest/vol_labels.py
"""
Outcome labelling for VOLATILITY signals.

`labels.py` scores a signal by the signed return of the underlying. That is the
right target for BULLISH and BEARISH and meaningless for SHORT_VOL and LONG_VOL:
a short-vol position pays when realised volatility comes in BELOW the implied
level it was sold at, whichever way the index moves. Scoring a vol signal by
direction measures something it never claimed to predict, and returns NO_EDGE
for reasons that have nothing to do with whether the signal works.

THE LABEL
    vol_miss = (IV at entry - realised vol over the horizon), in vol points.
    Positive means implied exceeded realised, so selling vol was right.
    Signed by the position: SHORT_VOL keeps the sign, LONG_VOL flips it, so a
    positive label always means "the signal was right" exactly as in the
    directional labeller.

THIS IS THE EX-POST PREMIUM, and it is deliberately not the same quantity the
VRP signal uses as its input. The signal reads implied against vol ALREADY
realised, which is observable now. This measures implied against vol realised
AFTERWARDS, which is the thing that actually pays and cannot be known at entry.
Using the contemporaneous spread as both input and outcome would be circular.

TENOR
    The IV is a 30-calendar-day constant-maturity number, so the 20-session
    horizon (~28 calendar days) is the tenor-matched one and is the honest
    headline. The shorter horizons ask a different, weaker question: did the
    signal anticipate NEAR-term vol. They are reported because the decay profile
    is informative, not because a 5-session realisation is comparable to a
    30-day implied.
"""
from typing import Optional

from backtest.labels import ForwardReturns
from realized_vol import compute_realized_vol

# Fixed in advance, like the directional horizons. Counted in trading sessions.
HORIZONS_VOL_SESSIONS = (5, 10, 20)

# Realised vol over fewer sessions than this is too noisy to read as a path
# point, so the intermediate excursion track starts here.
MIN_PATH_SESSIONS = 5

# Directions this labeller applies to.
VOL_DIRECTIONS = {"SHORT_VOL", "LONG_VOL"}


def vol_horizon_keys(horizons: tuple[int, ...] = HORIZONS_VOL_SESSIONS) -> list[str]:
    return [f"{h}d_vol" for h in horizons]


def forward_realised_vol(closes: list[float], index: int,
                         sessions: int) -> Optional[float]:
    """
    Annualised volatility realised over the `sessions` closes AFTER `index`.

    Strictly forward-looking: the entry close is the base of the first return
    and no close at or before it contributes to the estimate beyond that.
    Returns None when the horizon runs past the data rather than shortening it.
    """
    end = index + sessions
    if index < 0 or end >= len(closes):
        return None
    window = closes[index:end + 1]          # sessions+1 prices -> sessions returns
    return compute_realized_vol(window, window=sessions)


def direction_sign(direction: str) -> int:
    """+1 for SHORT_VOL (paid when implied exceeded realised), -1 for LONG_VOL."""
    return 1 if direction == "SHORT_VOL" else -1


def compute_vol_outcome(
    index: int,
    dates: list[str],
    closes: list[float],
    iv_by_date: dict[str, float],
    symbol: str,
    direction: str = "SHORT_VOL",
    horizons: tuple[int, ...] = HORIZONS_VOL_SESSIONS,
) -> Optional[ForwardReturns]:
    """
    Label one observation by the vol it was right or wrong about.

    Args:
        iv_by_date: entry implied vol per session date, as a decimal (0.16).
            Must be the same constant-maturity series the signal read, or the
            miss measures the tenor difference rather than the premium.
        direction: "SHORT_VOL" or "LONG_VOL".

    Returns None when the entry date carries no IV — a vol outcome without an
    entry implied level is not a vol outcome. Reuses `ForwardReturns` so the
    metrics and benchmark code works unchanged; the values are VOL POINTS, not
    basis points, which `BacktestRun.unit` reports.
    """
    entry_date = dates[index]
    iv = iv_by_date.get(entry_date)
    if iv is None:
        return None

    sign = direction_sign(direction)
    iv_points = iv * 100.0

    returns: dict[str, Optional[float]] = {}
    mae: dict[str, Optional[float]] = {}
    mfe: dict[str, Optional[float]] = {}

    for h in horizons:
        key = f"{h}d_vol"
        rv = forward_realised_vol(closes, index, h)
        if rv is None:
            returns[key] = mae[key] = mfe[key] = None
            continue

        returns[key] = round(sign * (iv_points - rv * 100.0), 3)

        # Intermediate track: the miss as it stood partway through the horizon.
        # For a vol position this is the analogue of drawdown — it shows whether
        # the trade was underwater before it came good.
        path = []
        for k in range(MIN_PATH_SESSIONS, h + 1):
            rv_k = forward_realised_vol(closes, index, k)
            if rv_k is not None:
                path.append(sign * (iv_points - rv_k * 100.0))
        mae[key] = round(min(path), 3) if path else None
        mfe[key] = round(max(path), 3) if path else None

    return ForwardReturns(
        ts=entry_date,
        symbol=symbol,
        spot_at_signal=iv_points,     # the entry implied level, in vol points
        returns_bps=returns,
        mae_bps=mae,
        mfe_bps=mfe,
    )


def build_vol_null_labels(
    picks: list[int],
    dates: list[str],
    closes: list[float],
    iv_by_date: dict[str, float],
    symbol: str,
    directions: Optional[list[str]] = None,
    horizons: tuple[int, ...] = HORIZONS_VOL_SESSIONS,
) -> list[ForwardReturns]:
    """
    Label drawn sessions exactly as the real signals were.

    The null answers the question that matters for a vol signal: over these same
    weekdays, how often did implied simply exceed realised anyway? The variance
    risk premium is positive most of the time, so a short-vol signal that "wins"
    70% of the time may only be collecting the premium that was there for the
    taking on any random day. Without this column that is invisible.
    """
    out = []
    for n, idx in enumerate(picks):
        direction = directions[n] if directions and n < len(directions) else "SHORT_VOL"
        lab = compute_vol_outcome(idx, dates, closes, iv_by_date, symbol,
                                  direction, horizons)
        if lab is not None:
            out.append(lab)
    return out


def iv_by_date_from_extras(extras: Optional[dict]) -> dict[str, float]:
    """
    Pull the entry-IV series out of the backtest's extras.

    Accepts `iv_series` ([{date, iv}]) or falls back to `vrp_series`, which
    already carries an `iv` per date. Returns an empty mapping rather than
    raising: the engine reports "no IV series" as a labelling failure with a
    reason, which is more useful than a traceback.
    """
    if not extras:
        return {}
    series = extras.get("iv_series") or extras.get("vrp_series") or []
    out: dict[str, float] = {}
    for row in series:
        d, iv = row.get("date"), row.get("iv")
        if d and iv is not None:
            out[d] = float(iv)
    return out
