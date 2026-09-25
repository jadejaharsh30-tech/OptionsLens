# optionslens/backend/skew.py
"""
The 25-delta risk reversal: IV(25-delta call) minus IV(25-delta put), in vol points.

WHAT IT MEASURES
    Not the level of volatility but its PRICE ASYMMETRY. Index risk reversals
    are persistently negative — downside strikes trade at a higher implied vol
    than equidistant upside ones, because the demand for crash protection is
    structural and one-sided. The number is therefore almost never interesting
    in absolute terms; what matters is where today's sits inside its own
    history, which is why everything here is percentile-ranked.

WHY 25 DELTA AND NOT A FIXED MONEYNESS
    A fixed percentage out of the money means a different probability of
    exercise on every day, because the distance scales with volatility. Delta
    already carries that scaling, so a 25-delta wing is comparable across a
    calm week and a violent one. `features._risk_reversal_25d` additionally
    REFUSES to report a reading when the nearest available strike is more than
    0.10 delta away from 0.25 — on a thin chain the "25-delta wing" can be the
    5-delta wing, and a risk reversal built from it is a different quantity
    wearing the same name.

WHY THE SERIES IS PRECOMPUTED HERE
    The signal needs a percentile over hundreds of sessions, and deriving a risk
    reversal means solving IV and delta across a whole chain. Recomputing the
    whole window on every bar would be that work repeated once per bar. Building
    the dated series in one pass and handing it to the signal through `extras`
    is the same route `vrp` and `term_structure` take, and it keeps the signal
    itself free of any database.

    One reading per SESSION, taken from that session's last bar. Mixing a 10:15
    reading into a series of closing readings would rank a time of day as though
    it were a regime — index skew has its own intraday shape.

Pure functions over snapshots. The store read lives in `load_skew_history`,
which the live path and the backtest fixture both call.
"""
from typing import Iterable, Optional

from eod_vol import percentile_rank
from recorder.models import ChainSnapshot

# Below this many observations a percentile is not a percentile. Matches
# vrp.MIN_OBSERVATIONS so the three ranked signals are read on the same scale.
MIN_OBSERVATIONS = 60


def rr25_from_snapshot(snapshot: ChainSnapshot) -> Optional[float]:
    """
    The 25-delta risk reversal for one snapshot, in vol points, or None.

    The single definition. The signal's current reading and every historical
    reading it ranks against both come through here, so a difference in the
    series can only be a difference in the market — not in how the number was
    derived, which is the failure mode that made IV Rank compare a live value
    against a differently-built past.

    None is the honest answer on a chain with no solvable wings, and it is
    common on EOD stock data where the far strikes simply did not trade.
    """
    from signals.features import compute_features

    return compute_features(snapshot).rr_25d


def build_skew_series(snapshots: Iterable[ChainSnapshot]) -> list[dict]:
    """
    One risk-reversal reading per session date, from each session's LAST bar.

    Takes the last bar rather than the first because the close is the reading
    every other daily series here is stamped at — `spot_history`, the IV
    snapshot and the EOD adapter all sit at the session close — and a signal
    that mixes measurement times has a calendar artefact in it.

    Dates whose chain yields no usable 25-delta pair are omitted, not carried
    forward. A stale repeated value would be counted as a fresh observation by
    the percentile and would pull the distribution toward whatever the last
    liquid day happened to print.

    Returns [{"date", "rr_25d"}] ascending, rr_25d in VOL POINTS.
    """
    last_by_date: dict[str, ChainSnapshot] = {}
    for snap in snapshots:
        prev = last_by_date.get(snap.session_date)
        if prev is None or snap.ts >= prev.ts:
            last_by_date[snap.session_date] = snap

    out = []
    for d in sorted(last_by_date):
        rr = rr25_from_snapshot(last_by_date[d])
        if rr is None:
            continue
        out.append({"date": d, "rr_25d": round(rr, 4)})
    return out


def skew_percentile(series: list[dict], current_rr: float,
                    min_observations: int = MIN_OBSERVATIONS) -> Optional[float]:
    """
    Where `current_rr` sits in the historical risk reversal, 0-100.

    Percentile rank rather than a z-score: the risk-reversal distribution has a
    long left tail by construction (crash protection gets bid, it does not get
    offered), so a standard deviation is not a natural unit for it and a
    two-sigma threshold means something different on each side.
    """
    values = [r["rr_25d"] for r in series if r.get("rr_25d") is not None]
    if len(values) < min_observations:
        return None
    return percentile_rank(values, current_rr)


def load_skew_history(db_path: Optional[str], symbol: str,
                      dates: Optional[list[str]] = None) -> list[dict]:
    """
    Build the dated risk-reversal series for a symbol from recorded snapshots.

    Reads the same store the backtester replays, so the history a signal ranks
    against is derived from exactly the bars it will be evaluated on. Pass
    `db_path` to read a materialised EOD database; omit it for the live
    recorder's store.

    Pulls one bar per date through `last_snapshot_of_session` rather than
    walking each session: on a year of minute-level recorder data the walk is
    millions of rows loaded to keep 250 of them.
    """
    from recorder.store import last_snapshot_of_session, recorded_dates

    kwargs = {"db_path": db_path} if db_path else {}
    days = sorted(dates) if dates is not None else recorded_dates(symbol, **kwargs)

    def _closes() -> Iterable[ChainSnapshot]:
        for d in days:
            snap = last_snapshot_of_session(symbol, d, **kwargs)
            if snap is not None:
                yield snap

    return build_skew_series(_closes())


def summarise(series: list[dict]) -> dict:
    """
    Diagnostics for the API and the daily digest.

    `pct_negative` is the sanity check: on an index it should be close to 100.
    A series hovering near 50 means the wings being picked are not what the
    name says — most likely a thin chain where the nearest strike to 25 delta
    is landing on a different part of the curve each day.
    """
    if not series:
        return {"observations": 0,
                "note": ("No sessions yielded a 25-delta risk reversal. The "
                         "wings must have solvable IV within 0.10 delta of "
                         "0.25 — thin EOD chains often do not.")}
    values = [r["rr_25d"] for r in series]
    return {
        "observations": len(series),
        "first_date":   series[0]["date"],
        "last_date":    series[-1]["date"],
        "latest_rr":    series[-1]["rr_25d"],
        "mean_rr":      round(sum(values) / len(values), 3),
        "pct_negative": round(sum(1 for v in values if v < 0) / len(values) * 100, 1),
    }
