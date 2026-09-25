# optionslens/backend/vrp.py
"""
Variance risk premium: implied volatility minus subsequently realised volatility.

VRP is the most robustly documented premium in options, and it is the one signal
here whose ingredients already exist and are already correct: `get_cm_iv_history`
gives 30-day constant-maturity ATM IV (not front-expiry, which is mostly the
weekly roll), and `spot_history` gives exchange closes with real dates.

TENOR MATCHING IS THE WHOLE GAME
    IV is a 30-calendar-day forward-looking number. RV over a 20-session window
    covers roughly 28 calendar days, which is the closest match available from
    daily closes — this is why the constant-maturity tenor and the RV window are
    a pair, and why changing one without the other quietly breaks the spread.

WHAT THIS IS NOT
    This computes the spread *contemporaneously*: today's implied against
    volatility realised over the window ENDING today. That is the standard
    tradeable formulation — you can observe it and act on it now. It is not the
    academic ex-post VRP, which compares today's implied against volatility
    realised over the following 30 days and cannot be known until they pass.

Pure functions over plain series. No broker, no FastAPI. The database reads live
in `load_vrp_history`, which both the live endpoint and the backtest fixture
call so the two cannot diverge.
"""
from typing import Optional

from eod_vol import DEFAULT_TARGET_DAYS, percentile_rank
from realized_vol import rv_series_from_dated_closes

# Paired with DEFAULT_TARGET_DAYS (30 calendar days). 20 sessions is ~28
# calendar days, the closest match daily closes allow.
DEFAULT_RV_WINDOW = 20

# Below this many paired observations a percentile is not a percentile.
MIN_OBSERVATIONS = 60


def build_vrp_series(iv_history: list[dict],
                     rv_history: list[dict]) -> list[dict]:
    """
    Join IV and RV on DATE, keeping only dates present in both.

    An inner join is the point: the old RV series invented its dates, so pairing
    by position silently offset the two after every exchange holiday. Joining on
    the date means a missing reading produces a gap, which is honest, rather
    than a shifted series, which is wrong and invisible.

    Args:
        iv_history: [{"date", "iv"}] — decimals, e.g. 0.14
        rv_history: [{"date", "rv"}] — decimals

    Returns [{"date", "iv", "rv", "vrp"}] ascending, vrp in VOL POINTS
    (iv - rv, x100) because that is how a vol trader reads it.
    """
    rv_by_date = {r["date"]: r["rv"] for r in rv_history
                  if r.get("date") and r.get("rv") is not None}

    out = []
    for row in iv_history:
        d, iv = row.get("date"), row.get("iv")
        if not d or iv is None:
            continue
        rv = rv_by_date.get(d)
        if rv is None:
            continue
        out.append({"date": d, "iv": iv, "rv": rv,
                    "vrp": round((iv - rv) * 100.0, 4)})
    out.sort(key=lambda r: r["date"])
    return out


def observations_before(series: list[dict], as_of_date: str) -> list[dict]:
    """
    The part of `series` strictly BEFORE `as_of_date`.

    Look-ahead control. A signal ranking today's VRP against its history must
    not include today, and must never see later dates. Keeping this in one
    tested function means every caller gets it right, rather than each
    re-implementing a date comparison that is easy to get subtly wrong (`<=`
    instead of `<` would leak the current observation into its own percentile).
    """
    return [r for r in series if r.get("date") and r["date"] < as_of_date]


def vrp_percentile(series: list[dict], current_vrp: float,
                   min_observations: int = MIN_OBSERVATIONS) -> Optional[float]:
    """
    Where `current_vrp` sits in the historical spread, 0-100.

    Percentile rank, not (x - low)/(high - low): one crisis day would otherwise
    set the range for a year and pin every later reading near zero.
    """
    values = [r["vrp"] for r in series if r.get("vrp") is not None]
    if len(values) < min_observations:
        return None
    return percentile_rank(values, current_vrp)


def load_vrp_history(db_path: str, symbol: str, days: int = 504,
                     rv_window: int = DEFAULT_RV_WINDOW,
                     target_days: float = DEFAULT_TARGET_DAYS) -> list[dict]:
    """
    Build the VRP series for a symbol from the app's own stores.

    Called by both the live endpoint and the backtest fixture so the two cannot
    compute the spread differently — the same failure mode that made IV Rank
    compare a live reading against differently-built history.

    `days` is a window of trading dates, defaulting to about two years so a
    percentile has something to stand on.
    """
    from snapshot_store import get_cm_iv_history, get_spot_history

    iv_history = get_cm_iv_history(db_path, symbol, days, target_days)
    iv_history = sorted(iv_history, key=lambda r: r["date"])

    # Extra closes so the first RV window is complete rather than truncated.
    spots = get_spot_history(db_path, symbol, days + rv_window + 5)
    rv_history = rv_series_from_dated_closes(
        sorted(spots, key=lambda r: r["date"]), window=rv_window)

    return build_vrp_series(iv_history, rv_history)


def summarise(series: list[dict]) -> dict:
    """Diagnostics for the API and the daily digest."""
    if not series:
        return {"observations": 0,
                "note": "No paired IV/RV observations. Has the bhavcopy import run?"}
    latest = series[-1]
    values = [r["vrp"] for r in series]
    return {
        "observations": len(series),
        "first_date":   series[0]["date"],
        "last_date":    latest["date"],
        "latest_iv":    round(latest["iv"] * 100, 2),
        "latest_rv":    round(latest["rv"] * 100, 2),
        "latest_vrp":   latest["vrp"],
        "mean_vrp":     round(sum(values) / len(values), 3),
        "pct_positive": round(sum(1 for v in values if v > 0) / len(values) * 100, 1),
    }
