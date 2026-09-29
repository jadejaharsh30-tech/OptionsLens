# optionslens/backend/cas.py
"""
Closing-auction dislocation: how far the auction moved the close away from the
last continuous price, and how much of that the derivatives market agreed with
(roadmap item 34).

WHAT CAS CHANGED
    Since 3 Aug 2026, F&O-eligible cash stocks stop continuous trading at 15:15
    and the official close is the equilibrium price of a call auction ending
    ~15:35. The index close is built from those constituent closes, so the
    index is frozen and then re-priced by the auction in the same way. That
    creates a measurable gap that did not exist before: the close versus the
    last price anyone could actually trade at.

THE HYPOTHESIS
    Closing auctions concentrate flow that has to execute at the close — index
    rebalancing, benchmarked funds, closing-price orders — and that flow can
    push the print away from where the market would otherwise have settled. If
    so, part of the gap is price pressure, and price pressure reverts: an
    auction that printed well above the last continuous price should see some
    of it given back by the next close. Documented for closing auctions in other
    markets; new here, and unmeasured.

TWO MEASURES, BECAUSE THE RAW ONE CONFLATES TWO THINGS
    raw_bps       ln(auction print / last continuous price). Includes whatever
                  genuinely happened in the market during those twenty minutes
                  — news, a futures sell-off — which is not a dislocation at
                  all and has no reason to revert.
    adjusted_bps  raw minus the FUTURE's log return over the same window.
                  Futures keep trading continuously through the auction, so
                  their move is the market's own repricing; what is left is the
                  part of the print the derivatives market did not agree with.
                  This is the quantity the hypothesis is actually about.

    The adjusted measure needs futures on both bars, and futures capture began
    with item 15, so its history starts now. The signal ranks whichever measure
    it is told to and NEVER mixes them in one percentile: a series that is raw
    on some dates and adjusted on others is two definitions pretending to be
    one, the failure that made IV Rank compare incomparable readings.

WHERE THE NUMBERS COME FROM
    Only the recorder's own bars: the last CONTINUOUS bar (the last price before
    the auction) and the first POST_CAS bar (the first observation after it).
    Selected by the `session_phase` each bar was tagged with at capture, not by
    reading the clock — that tag exists precisely so nothing has to infer the
    phase later.

    The official close captured at 15:50 (`eod_close`) is NOT used as the close
    here, because live it does not exist yet when the signal runs at ~15:36;
    using it in the backtest would test a signal that cannot run live. It is
    joined in afterwards as a DATA CHECK: the post-auction print and the
    official close should agree, and `summarise` reports by how much.
"""
import math
import sqlite3
from typing import Iterable, Optional

from eod_vol import percentile_rank
from market_hours import CAS_LIVE_DATE, SessionPhase

MEASURES = ("raw_bps", "adjusted_bps")

# Matches the other ranked signals, so their percentiles are read on one scale.
MIN_OBSERVATIONS = 60


def _phase(bar) -> SessionPhase:
    p = bar["session_phase"] if isinstance(bar, dict) else bar.session_phase
    return p if isinstance(p, SessionPhase) else SessionPhase(p)


def _get(bar, name):
    return bar.get(name) if isinstance(bar, dict) else getattr(bar, name, None)


def auction_reading(bars: Iterable) -> Optional[dict]:
    """
    The auction reading for ONE session, from its bars in time order.

    `bars` may be `ChainSnapshot`s (the signal's replay window) or plain dicts
    with the same field names (the loader's chain_meta rows); both go through
    this one function so the live reading and the history it is ranked against
    are the same quantity.

    Returns None when either side of the auction is missing, or the session
    predates CAS. Returns a reading with `adjusted_bps=None` when the futures
    cannot be compared — absent on either bar, or a different contract on each,
    which happens on monthly expiry day when the front future rolls at 15:30
    and the post-auction bar quotes next month's.
    """
    pre = post = None
    for bar in bars:
        phase = _phase(bar)
        if phase is SessionPhase.CONTINUOUS:
            pre = bar                       # keep the LAST continuous bar
        elif phase is SessionPhase.POST_CAS and post is None:
            post = bar                      # and the FIRST post-auction bar
    if pre is None or post is None:
        return None

    session_date = _get(post, "session_date")
    if session_date is None or session_date < CAS_LIVE_DATE.isoformat():
        return None
    if _get(pre, "session_date") != session_date:
        return None

    s0, s1 = _get(pre, "spot"), _get(post, "spot")
    if not s0 or not s1 or s0 <= 0 or s1 <= 0:
        return None
    raw = math.log(s1 / s0) * 1e4

    f0, f1 = _get(pre, "futures"), _get(post, "futures")
    same_contract = (_get(pre, "futures_expiry") is not None
                     and _get(pre, "futures_expiry") == _get(post, "futures_expiry"))
    adjusted = None
    futures_bps = None
    if f0 and f1 and f0 > 0 and f1 > 0 and same_contract:
        futures_bps = math.log(f1 / f0) * 1e4
        adjusted = raw - futures_bps

    return {
        "date":           session_date,
        "pre_ts":         _get(pre, "ts"),
        "post_ts":        _get(post, "ts"),
        "pre_spot":       s0,
        "auction_print":  s1,
        "raw_bps":        round(raw, 4),
        "futures_bps":    None if futures_bps is None else round(futures_bps, 4),
        "adjusted_bps":   None if adjusted is None else round(adjusted, 4),
    }


def build_cas_series(bars: Iterable) -> list[dict]:
    """One reading per session, ascending. `bars` in time order, any sessions."""
    by_date: dict[str, list] = {}
    for bar in bars:
        by_date.setdefault(_get(bar, "session_date"), []).append(bar)

    out = []
    for d in sorted(k for k in by_date if k):
        reading = auction_reading(by_date[d])
        if reading is not None:
            out.append(reading)
    return out


def measure_history(series: list[dict], measure: str) -> list[float]:
    """The values of ONE measure, dropping dates that lack it. Never mixes the two."""
    if measure not in MEASURES:
        raise ValueError(f"unknown measure {measure!r}; expected one of {MEASURES}")
    return [r[measure] for r in series if r.get(measure) is not None]


def dislocation_percentile(series: list[dict], current: float, measure: str,
                           min_observations: int = MIN_OBSERVATIONS,
                           ) -> Optional[float]:
    values = measure_history(series, measure)
    if len(values) < min_observations:
        return None
    return percentile_rank(values, current)


def load_cas_history(symbol: str, db_path: Optional[str] = None) -> list[dict]:
    """
    Build the series from the recorder's `chain_meta` alone — spot, futures,
    phase and timestamp are all on the header, so no strike rows are loaded.

    Several expiries share a timestamp and carry the same spot and future, so
    rows are collapsed to one per timestamp. The official close from
    `eod_close`, when captured, is attached for the data check only.
    """
    from config import MARKET_DATA_DB

    conn = sqlite3.connect(db_path or MARKET_DATA_DB)
    conn.row_factory = sqlite3.Row
    try:
        bars = conn.execute("""
            SELECT ts, session_date, session_phase, spot, futures, futures_expiry
            FROM chain_meta
            WHERE symbol = ? AND session_date >= ?
            GROUP BY ts
            ORDER BY ts
        """, (symbol, CAS_LIVE_DATE.isoformat())).fetchall()
        closes = {r[0]: r[1] for r in conn.execute(
            "SELECT session_date, close_price FROM eod_close WHERE symbol = ?",
            (symbol,))}
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    series = build_cas_series(dict(b) for b in bars)
    for r in series:
        official = closes.get(r["date"])
        if official and official > 0:
            r["official_close"] = official
            r["print_vs_official_bps"] = round(
                math.log(r["auction_print"] / official) * 1e4, 4)
    return series


def summarise(series: list[dict]) -> dict:
    """
    Diagnostics for the API and the report.

    `max_print_gap_bps` is the check to read first. The post-auction print is
    standing in for the official close; if the two differ by more than a basis
    point or two, the first POST_CAS bar is not seeing the auction price yet
    and the whole series is measuring the wrong thing.
    """
    if not series:
        return {"observations": 0,
                "note": ("No sessions with both a last continuous bar and a "
                         "post-auction bar since CAS went live. The recorder "
                         "must run through 15:40 for this to accumulate.")}
    raw = [abs(r["raw_bps"]) for r in series]
    adj = [abs(r["adjusted_bps"]) for r in series if r.get("adjusted_bps") is not None]
    gaps = [abs(r["print_vs_official_bps"]) for r in series
            if r.get("print_vs_official_bps") is not None]
    return {
        "observations":        len(series),
        "with_futures":        len(adj),
        "first_date":          series[0]["date"],
        "last_date":           series[-1]["date"],
        "mean_abs_raw_bps":    round(sum(raw) / len(raw), 3),
        "mean_abs_adjusted_bps": round(sum(adj) / len(adj), 3) if adj else None,
        "checked_against_official": len(gaps),
        "max_print_gap_bps":   round(max(gaps), 3) if gaps else None,
    }
