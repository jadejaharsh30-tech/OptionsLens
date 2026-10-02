# optionslens/backend/backtest/extras.py
"""
The history a signal cannot reach on its own, built once for the API and the
command line alike.

A signal sees ChainSnapshots only, which is what makes live and backtest
identical. But several signals rank today against a daily series no snapshot
contains, and vol labelling needs the implied level at entry. This module is
the ONE place those series are assembled, so `/api/backtest/run` and
`python -m backtest.cli` cannot hand a signal different histories.
"""
import logging
from dataclasses import dataclass
from typing import Any, Optional

import config

logger = logging.getLogger(__name__)


# Load every date the stores hold. The loaders' own default (504 dates) was
# the RANKING window leaking into the DATA window: with 2008-2024 imported,
# every session before the last two years would have found no reading and
# skipped. The ranking window is now explicit in each signal (`lookback`), so
# the data can be complete without changing what a signal compares against.
ALL_HISTORY_DAYS = 100_000


@dataclass(frozen=True)
class _Sources:
    app_db: str
    snapshot_db: Optional[str]
    # Set only by a caller evaluating ONE date (the daily runner): series that
    # are expensive to rebuild stop once they hold `max_prior_readings` before
    # `until`. See skew.load_skew_history for why that is exact, not approximate.
    until: Optional[str] = None
    max_prior_readings: Optional[int] = None


def build_extras(symbol: str, signal_id: Optional[str] = None,
                 app_db: Optional[str] = None,
                 snapshot_db: Optional[str] = None,
                 ) -> tuple[dict, list[str]]:
    """One signal's history; see `build_extras_for` for what is built."""
    return build_extras_for(symbol, [signal_id] if signal_id else [],
                            app_db=app_db, snapshot_db=snapshot_db)


def build_extras_for(symbol: str, signal_ids: list[str],
                     app_db: Optional[str] = None,
                     snapshot_db: Optional[str] = None,
                     until: Optional[str] = None,
                     max_prior_readings: Optional[int] = None,
                     ) -> tuple[dict, list[str]]:
    """
    Assemble the history a signal cannot reach on its own.

    A signal sees ChainSnapshots only, which is what makes live and backtest
    identical — but VRP ranks against two years of daily readings, the term
    structure spans expiries a single snapshot does not contain, the skew
    percentile needs a chain-wide solve per session, and vol labelling needs the
    implied level at entry. All of it is built here through the same loaders any
    script would call, so the API cannot compute a different history.

    Each series is built independently: a missing far-month leg must not also
    blank the VRP spread. Every outcome produces a note, because a signal that
    silently skips every bar for want of a series is the least debuggable
    outcome there is.

    The VRP series is always built: `iv_series` is what vol labelling scores
    ANY volatility signal against, whichever one is being run. Every other
    series is built only for the signal that consumes it (`SIGNAL_SERIES`), so
    a chain-wide IV solve for skew is not charged to a gex_regime run.

    Two stores, because the daily series and the replayed bars can live in
    different files:
      app_db       IV and spot history (default `config.DB_PATH`)
      snapshot_db  the snapshots being replayed (default: the recorder's store;
                   pass the materialised EOD file for a history run), which the
                   skew and CAS series are built from so they describe exactly
                   the bars the signal will be evaluated on

    Several signals at once (the daily runner) share the one VRP load rather
    than repeating it per signal.
    """
    extras: dict[str, Any] = {}
    notes: list[str] = []

    src = _Sources(app_db or config.DB_PATH, snapshot_db, until, max_prior_readings)
    _add_vrp_extras(symbol, extras, notes, src)
    for signal_id in dict.fromkeys(signal_ids):
        builder = SIGNAL_SERIES.get(signal_id or "")
        if builder is not None:
            builder(symbol, extras, notes, src)
    return extras, notes


def _add_vrp_extras(symbol: str, extras: dict, notes: list[str],
                     src: "_Sources") -> None:
    """VRP spread, and the entry-IV series vol labelling scores against."""
    from vrp import load_vrp_history, summarise

    try:
        series = load_vrp_history(src.app_db, symbol, days=ALL_HISTORY_DAYS)
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"VRP history unavailable for {symbol}: {e!r}")
        notes.append(f"VRP history could not be loaded: {e}")
        return

    if not series:
        notes.append(
            f"No paired IV/RV history for {symbol}, so vrp will skip every bar "
            f"and vol outcomes cannot be scored. Run the bhavcopy import "
            f"(docs/BHAVCOPY.md) to populate atm_iv_history and spot_history.")
        return

    info = summarise(series)
    notes.append(
        f"VRP history: {info['observations']} paired observations, "
        f"{info['first_date']} to {info['last_date']}.")
    extras["vrp_series"] = series
    extras["iv_series"] = [{"date": r["date"], "iv": r["iv"]} for r in series]


def _add_term_structure_extras(symbol: str, extras: dict, notes: list[str],
                                src: "_Sources") -> None:
    """
    60-day minus 30-day constant-maturity IV.

    The far leg is the one that fails, and it fails silently: it needs a traded
    expiry beyond two months, which monthly-only symbols often do not have. The
    note reports its coverage against the near leg so a thin series is visible
    as thin rather than read as a full history.
    """
    from term_structure import coverage, load_term_structure_history, summarise

    try:
        series = load_term_structure_history(src.app_db, symbol, days=ALL_HISTORY_DAYS)
        cov = coverage(src.app_db, symbol, days=ALL_HISTORY_DAYS)
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"Term structure unavailable for {symbol}: {e!r}")
        notes.append(f"Term-structure history could not be loaded: {e}")
        return

    if not series:
        notes.append(
            f"No term-structure history for {symbol} ({cov['near_dates']} dates "
            f"reach 30 days, {cov['far_dates']} reach 60), so term_structure "
            f"will skip every bar. The far leg needs a traded expiry beyond two "
            f"months.")
        return

    info = summarise(series)
    notes.append(
        f"Term structure: {info['observations']} paired observations "
        f"({cov['far_leg_coverage_pct']}% of dates with a 30-day reading also "
        f"reach 60 days), {info['first_date']} to {info['last_date']}, "
        f"{info['pct_inverted']}% inverted.")
    extras["term_structure_series"] = series


def _add_skew_extras(symbol: str, extras: dict, notes: list[str],
                      src: "_Sources") -> None:
    """
    25-delta risk reversal, one reading per recorded session.

    Built from the recorder's own store rather than a daily table, because no
    daily table holds per-strike IV for the bhavcopy backfill — only the live
    snapshot job writes one, and it does not reach back.
    """
    from skew import load_skew_history, summarise

    try:
        series = load_skew_history(src.snapshot_db, symbol, until=src.until,
                                   max_prior_readings=src.max_prior_readings)
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"Skew history unavailable for {symbol}: {e!r}")
        notes.append(f"Skew history could not be loaded: {e}")
        return

    if not series:
        notes.append(
            f"No 25-delta risk-reversal history for {symbol}, so skew_rr25 will "
            f"skip every bar. The wings must have solvable IV within 0.10 delta "
            f"of 0.25, which thin chains often do not.")
        return

    info = summarise(series)
    notes.append(
        f"Skew history: {info['observations']} sessions, {info['first_date']} "
        f"to {info['last_date']}, {info['pct_negative']}% with puts bid.")
    extras["skew_series"] = series


def _add_cas_extras(symbol: str, extras: dict, notes: list[str],
                     src: "_Sources") -> None:
    """
    Closing-auction readings from the recorder's own bars.

    Reports how many sessions carry the futures-adjusted measure separately,
    because the signal ranks one measure at a time and the adjusted one only
    starts accumulating with futures capture.
    """
    from cas import load_cas_history, summarise

    try:
        series = load_cas_history(symbol, src.snapshot_db)
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"CAS history unavailable for {symbol}: {e!r}")
        notes.append(f"CAS history could not be loaded: {e}")
        return

    if not series:
        notes.append(
            f"No closing-auction readings for {symbol}, so cas_dislocation will "
            f"skip every bar. The recorder must capture both the last continuous "
            f"bar and a post-auction bar (run it through 15:40).")
        return

    info = summarise(series)
    gap = (f" Post-auction print vs official close: at most "
           f"{info['max_print_gap_bps']} bps over {info['checked_against_official']} "
           f"sessions." if info["max_print_gap_bps"] is not None else "")
    notes.append(
        f"CAS history: {info['observations']} sessions ({info['with_futures']} "
        f"with the futures-adjusted measure), {info['first_date']} to "
        f"{info['last_date']}.{gap}")
    extras["cas_series"] = series


def _add_dispersion_extras(symbol: str, extras: dict, notes: list[str],
                            src: "_Sources") -> None:
    """
    Implied correlation of an index against its configured member basket.

    The note names the basket actually used, because a NIFTY series built from
    four stocks (a member missing from the import) is a different series from
    one built from five, and nothing else would say so.
    """
    from dispersion import load_dispersion_history, summarise

    try:
        series, info = load_dispersion_history(src.app_db, symbol, days=ALL_HISTORY_DAYS)
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"Dispersion history unavailable for {symbol}: {e!r}")
        notes.append(f"Dispersion history could not be loaded: {e}")
        return

    s = summarise(series, info)
    if not series:
        notes.append(
            f"No implied-correlation history for {symbol}, so dispersion will "
            f"skip every bar. {s['note']}")
        return

    excluded = (f" Excluded for thin coverage: {s['excluded']}."
                if s["excluded"] else "")
    notes.append(
        f"Dispersion: {s['observations']} dates from basket {s['basket']}, "
        f"{s['first_date']} to {s['last_date']}; ρ outside [0, 1] on "
        f"{s['pct_above_one'] + s['pct_below_zero']:.1f}% of dates (a proxy — "
        f"read only its percentile)."
        f"{excluded}")
    extras["dispersion_series"] = series


# Signal-specific series, built only when that signal is run. A series-backed
# signal missing from this map lists in the dropdown and never fires — the
# generic test in test_routers.py fails if one is added without an entry.
SIGNAL_SERIES = {
    "term_structure":  _add_term_structure_extras,
    "skew_rr25":       _add_skew_extras,
    "cas_dislocation": _add_cas_extras,
    "dispersion":      _add_dispersion_extras,
}

