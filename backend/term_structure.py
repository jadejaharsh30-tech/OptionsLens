# optionslens/backend/term_structure.py
"""
The implied-volatility term structure: far-tenor IV minus near-tenor IV.

WHAT THE SLOPE MEANS
    Index option curves are normally in CONTANGO — the 60-day implied sits above
    the 30-day, because uncertainty compounds with time and because the front is
    where premium is sold hardest. When that inverts (BACKWARDATION, a negative
    slope) the market is pricing more volatility over the next month than over
    the next two. That is the classic stress signature: it appears around
    results, policy events and drawdowns, and it disappears quickly.

    So the slope is not a level indicator. It is a measure of WHERE in the curve
    the fear is, which is information the 30-day number alone cannot carry: IV
    can be high with a normal curve (a repriced regime) or high with an inverted
    one (an imminent event). Those are different trades.

WHY BOTH LEGS ARE CONSTANT-MATURITY
    `eod_vol.constant_maturity_iv` interpolates in TOTAL VARIANCE between the
    expiries bracketing a target tenor. Taking "front expiry minus next expiry"
    instead would measure the weekly roll as much as the curve — the front leg
    would walk from 7 days to 1 day over a week while the back leg barely moved,
    manufacturing a slope out of the calendar. Both legs are pinned to fixed
    tenors so a change in the series is a change in the curve.

THE 60-DAY LEG IS THE FRAGILE ONE
    30 days is bracketed on essentially every NIFTY and BANKNIFTY session. 60
    days needs a second listed expiry beyond two months that actually traded,
    and on monthly-only underlyings (every single stock here) the far month is
    frequently untraded, so the slope is simply absent on those dates.
    `summarise` reports that coverage explicitly rather than letting a thin
    series pass as a full one — a percentile built from the 40% of dates where
    the far month happened to trade is a percentile of liquid days, not of the
    curve.

Pure functions over plain series. The database read lives in
`load_term_structure_history`, which the live path and the backtest fixture both
call so the two cannot build the curve differently — the same rule `vrp.py`
follows, and for the same reason.
"""
from typing import Optional

from eod_vol import percentile_rank

# Paired tenors. 30 days matches the constant-maturity IV everything else ranks
# (and the VIX convention); 60 is the nearest tenor that is reliably a DIFFERENT
# expiry rather than an interpolation between the same two points. Moving one
# without the other changes what the spread measures.
DEFAULT_NEAR_DAYS = 30.0
DEFAULT_FAR_DAYS = 60.0

# Below this many paired observations a percentile is not a percentile. Matches
# vrp.MIN_OBSERVATIONS on purpose: both rank a daily spread against its own
# history, so a reader comparing the two is comparing like with like.
MIN_OBSERVATIONS = 60


def build_slope_series(near_history: list[dict],
                       far_history: list[dict]) -> list[dict]:
    """
    Join the two constant-maturity IV series on DATE and take far minus near.

    An inner join, not a positional pair. The far leg has gaps the near leg does
    not — an untraded far month blanks a date — so zipping the two would pair
    today's front with last week's back and call the difference a slope. A
    missing leg produces a missing date here, which is honest.

    Args:
        near_history: [{"date", "iv"}] at the near tenor, iv as a decimal
        far_history:  [{"date", "iv"}] at the far tenor

    Returns [{"date", "near_iv", "far_iv", "slope"}] ascending, `slope` in VOL
    POINTS (far - near, x100) because that is the unit a vol trader reads and
    the same unit `vrp` reports its spread in.
    """
    far_by_date = {r["date"]: r["iv"] for r in far_history
                   if r.get("date") and r.get("iv") is not None}

    out = []
    for row in near_history:
        d, near_iv = row.get("date"), row.get("iv")
        if not d or near_iv is None:
            continue
        far_iv = far_by_date.get(d)
        if far_iv is None:
            continue
        out.append({
            "date":    d,
            "near_iv": near_iv,
            "far_iv":  far_iv,
            "slope":   round((far_iv - near_iv) * 100.0, 4),
        })
    out.sort(key=lambda r: r["date"])
    return out


def slope_percentile(series: list[dict], current_slope: float,
                     min_observations: int = MIN_OBSERVATIONS) -> Optional[float]:
    """
    Where `current_slope` sits in the historical curve, 0-100.

    Percentile rank against prior readings, not (x - low) / (high - low): one
    inverted crisis day sets the range for a year under the latter and pins
    every subsequent reading near the top.

    The percentile is what makes the slope comparable across symbols. A +1.5
    vol-point slope is routine on NIFTY and unusually steep on a single stock;
    only its own history says which.
    """
    values = [r["slope"] for r in series if r.get("slope") is not None]
    if len(values) < min_observations:
        return None
    return percentile_rank(values, current_slope)


def load_term_structure_history(db_path: str, symbol: str, days: int = 504,
                                near_days: float = DEFAULT_NEAR_DAYS,
                                far_days: float = DEFAULT_FAR_DAYS,
                                ) -> list[dict]:
    """
    Build the slope series for a symbol from `atm_iv_history`.

    Reads the same per-expiry table `get_cm_iv_history` ranks IV from, once per
    tenor, and joins. Both the API and any script go through here so the live
    reading and the backtested history cannot be interpolated differently —
    which is precisely the divergence that made IV Rank compare a live number
    against a differently-built past.

    `days` is a window of trading DATES, defaulting to about two years so a
    percentile has something to stand on.
    """
    from snapshot_store import get_cm_iv_history

    near = sorted(get_cm_iv_history(db_path, symbol, days, near_days),
                  key=lambda r: r["date"])
    far = sorted(get_cm_iv_history(db_path, symbol, days, far_days),
                 key=lambda r: r["date"])
    return build_slope_series(near, far)


def summarise(series: list[dict], near_days: float = DEFAULT_NEAR_DAYS,
              far_days: float = DEFAULT_FAR_DAYS) -> dict:
    """
    Diagnostics for the API and the daily digest.

    `pct_inverted` is the one to read first. On an index it should be small and
    clustered around events; if it is large, the far leg is probably being
    extrapolated rather than interpolated, and the "inversions" are an artefact
    of a curve that does not reach 60 days.
    """
    if not series:
        return {
            "observations": 0,
            "near_days": near_days,
            "far_days": far_days,
            "note": (f"No dates carry both a {near_days:.0f}-day and a "
                     f"{far_days:.0f}-day constant-maturity IV. The far leg "
                     f"needs a traded expiry beyond {far_days:.0f} days; on "
                     f"monthly-only symbols that is often absent. Has the "
                     f"bhavcopy import run?"),
        }

    latest = series[-1]
    values = [r["slope"] for r in series]
    return {
        "observations":  len(series),
        "near_days":     near_days,
        "far_days":      far_days,
        "first_date":    series[0]["date"],
        "last_date":     latest["date"],
        "latest_near":   round(latest["near_iv"] * 100, 2),
        "latest_far":    round(latest["far_iv"] * 100, 2),
        "latest_slope":  latest["slope"],
        "mean_slope":    round(sum(values) / len(values), 3),
        "pct_inverted":  round(sum(1 for v in values if v < 0) / len(values) * 100, 1),
    }


def coverage(db_path: str, symbol: str, days: int = 504,
             near_days: float = DEFAULT_NEAR_DAYS,
             far_days: float = DEFAULT_FAR_DAYS) -> dict:
    """
    How many dates each leg reaches, and how many carry both.

    Separated from `summarise` because the failure it detects is invisible in
    the joined series: a slope series of 120 dates looks healthy until you learn
    the near leg had 500 and the far leg blanked 380 of them. The percentile is
    then conditioned on the far month having traded, which correlates with
    exactly the volatile periods the signal is supposed to distinguish.
    """
    from snapshot_store import get_cm_iv_history

    near = get_cm_iv_history(db_path, symbol, days, near_days)
    far = get_cm_iv_history(db_path, symbol, days, far_days)
    near_dates = {r["date"] for r in near}
    far_dates = {r["date"] for r in far}
    both = near_dates & far_dates

    return {
        "symbol":      symbol,
        "near_dates":  len(near_dates),
        "far_dates":   len(far_dates),
        "paired":      len(both),
        "far_leg_coverage_pct": round(len(both) / len(near_dates) * 100, 1)
                                if near_dates else 0.0,
    }


def _report() -> None:
    """
    `python -m term_structure` — coverage per configured underlying.

    The question to answer before reading any term-structure backtest: does the
    60-day leg exist often enough for its percentile to mean anything? Printed
    as a table for the same reason the bhavcopy importer prints one — a number
    you have to write a query for is a number nobody checks.
    """
    from config import DB_PATH, UNDERLYINGS

    print(f"{'symbol':<12}{'30d dates':>11}{'60d dates':>11}{'paired':>9}"
          f"{'far leg':>9}  {'inverted':>9}  window")
    for symbol in UNDERLYINGS:
        cov = coverage(DB_PATH, symbol)
        series = load_term_structure_history(DB_PATH, symbol)
        info = summarise(series)
        window = (f"{info['first_date']} to {info['last_date']}"
                  if series else "-")
        print(f"{symbol:<12}{cov['near_dates']:>11}{cov['far_dates']:>11}"
              f"{cov['paired']:>9}{cov['far_leg_coverage_pct']:>8.1f}%"
              f"{(info.get('pct_inverted', 0.0)):>10.1f}%  {window}")

    print(f"\nA percentile needs {MIN_OBSERVATIONS} paired observations before "
          f"term_structure will fire at all.\nLow far-leg coverage means the "
          f"series is conditioned on the far month having traded,\nwhich is "
          f"not the same population as 'all sessions'.")


if __name__ == "__main__":
    _report()
