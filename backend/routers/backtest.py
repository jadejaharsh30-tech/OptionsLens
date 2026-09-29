# optionslens/backend/routers/backtest.py
"""
Signal and backtest API.

GET  /api/backtest/signals          → registered signals with versions + params
GET  /api/backtest/data-readiness   → whether enough data exists to conclude anything
POST /api/backtest/run              → run a signal against recorded data
GET  /api/backtest/evaluations/{id} → fire rate and skip-reason breakdown

`data-readiness` exists because the honest answer for the first few weeks is
"not yet". Surfacing that as a first-class response is better than returning a
confident-looking table computed from four sessions.
"""
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_token
from backtest.engine import run_backtest
from backtest.walkforward import describe_data_sufficiency
from config import DB_PATH
from recorder.store import recorded_dates
from signals.registry import get_signal, list_signals
from signals.store import evaluation_summary

router = APIRouter(prefix="/api/backtest", tags=["backtest"])
logger = logging.getLogger(__name__)

# Below this many sessions, results are plumbing checks rather than evidence.
MIN_SESSIONS_FOR_CONFIDENCE = 20


class RunRequest(BaseModel):
    signal_id:      str
    symbol:         str = "NIFTY"
    version:        Optional[int] = None
    params:         dict[str, Any] = {}
    session_dates:  Optional[list[str]] = None
    history_window: int = 60
    null_samples_per_signal: int = 10
    seed:           int = 42
    persist_evaluations: bool = True


@router.get("/signals")
def get_signals(token: str = Depends(get_token)):
    """Every registered signal, with its version and sweepable parameters."""
    return {
        "signals": [
            {
                "signal_id":      s.signal_id,
                "version":        s.version,
                "key":            s.key,
                "description":    s.description,
                "default_params": s.default_params,
                "min_history":    s.min_history,
            }
            for s in list_signals()
        ]
    }


@router.get("/data-readiness")
def data_readiness(symbol: str = "NIFTY", train_days: int = 20,
                   test_days: int = 10, token: str = Depends(get_token)):
    """
    How much recorded data exists and what it can support.

    The recorder accumulates one session per trading day and nothing can
    backfill it, so this is the gating constraint on all signal research.
    """
    dates = recorded_dates(symbol)
    sufficiency = describe_data_sufficiency(dates, train_days, test_days)

    return {
        "symbol":            symbol,
        "sessions_recorded": len(dates),
        "first_session":     dates[0] if dates else None,
        "last_session":      dates[-1] if dates else None,
        "can_run_backtest":  len(dates) >= 1,
        "statistically_meaningful": len(dates) >= MIN_SESSIONS_FOR_CONFIDENCE,
        "walk_forward":      sufficiency,
        "guidance": (
            "No data recorded yet — validate a token during market hours to "
            "start the recorder."
            if not dates else
            f"{len(dates)} session(s) recorded. Backtests will run, but below "
            f"{MIN_SESSIONS_FOR_CONFIDENCE} sessions treat every number as a "
            f"plumbing check rather than evidence of edge."
            if len(dates) < MIN_SESSIONS_FOR_CONFIDENCE else
            f"{len(dates)} sessions recorded — enough for provisional results."
        ),
    }


def _build_extras(symbol: str, signal_id: Optional[str] = None,
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
    series is built only for the signal that consumes it (`_SIGNAL_SERIES`), so
    a chain-wide IV solve for skew is not charged to a gex_regime run.
    """
    extras: dict[str, Any] = {}
    notes: list[str] = []

    _add_vrp_extras(symbol, extras, notes)
    builder = _SIGNAL_SERIES.get(signal_id or "")
    if builder is not None:
        builder(symbol, extras, notes)
    return extras, notes


def _add_vrp_extras(symbol: str, extras: dict, notes: list[str]) -> None:
    """VRP spread, and the entry-IV series vol labelling scores against."""
    from vrp import load_vrp_history, summarise

    try:
        series = load_vrp_history(DB_PATH, symbol)
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


def _add_term_structure_extras(symbol: str, extras: dict, notes: list[str]) -> None:
    """
    60-day minus 30-day constant-maturity IV.

    The far leg is the one that fails, and it fails silently: it needs a traded
    expiry beyond two months, which monthly-only symbols often do not have. The
    note reports its coverage against the near leg so a thin series is visible
    as thin rather than read as a full history.
    """
    from term_structure import coverage, load_term_structure_history, summarise

    try:
        series = load_term_structure_history(DB_PATH, symbol)
        cov = coverage(DB_PATH, symbol)
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


def _add_skew_extras(symbol: str, extras: dict, notes: list[str]) -> None:
    """
    25-delta risk reversal, one reading per recorded session.

    Built from the recorder's own store rather than a daily table, because no
    daily table holds per-strike IV for the bhavcopy backfill — only the live
    snapshot job writes one, and it does not reach back.
    """
    from skew import load_skew_history, summarise

    try:
        series = load_skew_history(None, symbol)
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


def _add_cas_extras(symbol: str, extras: dict, notes: list[str]) -> None:
    """
    Closing-auction readings from the recorder's own bars.

    Reports how many sessions carry the futures-adjusted measure separately,
    because the signal ranks one measure at a time and the adjusted one only
    starts accumulating with futures capture.
    """
    from cas import load_cas_history, summarise

    try:
        series = load_cas_history(symbol)
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


def _add_dispersion_extras(symbol: str, extras: dict, notes: list[str]) -> None:
    """
    Implied correlation of an index against its configured member basket.

    The note names the basket actually used, because a NIFTY series built from
    four stocks (a member missing from the import) is a different series from
    one built from five, and nothing else would say so.
    """
    from dispersion import load_dispersion_history, summarise

    try:
        series, info = load_dispersion_history(DB_PATH, symbol)
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
_SIGNAL_SERIES = {
    "term_structure":  _add_term_structure_extras,
    "skew_rr25":       _add_skew_extras,
    "cas_dislocation": _add_cas_extras,
    "dispersion":      _add_dispersion_extras,
}


@router.post("/run")
def run(req: RunRequest, token: str = Depends(get_token)):
    """Replay a signal over recorded data and compare it against a matched null."""
    try:
        get_signal(req.signal_id, req.version)
    except KeyError as e:
        raise HTTPException(404, str(e))

    dates = recorded_dates(req.symbol)
    if not dates:
        raise HTTPException(
            400,
            f"No recorded data for {req.symbol}. The recorder must run during "
            f"market hours before a backtest can be run.",
        )

    extras, extra_notes = _build_extras(req.symbol, req.signal_id)

    try:
        result = run_backtest(
            extras         = extras,
            signal_id      = req.signal_id,
            symbol         = req.symbol,
            version        = req.version,
            params         = req.params,
            session_dates  = req.session_dates,
            history_window = req.history_window,
            null_samples_per_signal = req.null_samples_per_signal,
            seed           = req.seed,
            persist_evaluations = req.persist_evaluations,
        )
    except Exception as e:
        logger.exception("Backtest failed")
        raise HTTPException(500, f"Backtest failed: {e}")

    payload = result.to_dict()
    payload["notes"] = extra_notes + payload.get("notes", [])
    return payload


class CorrelationRequest(BaseModel):
    symbol:         str = "NIFTY"
    signal_ids:     Optional[list[str]] = None     # default: every registered signal
    session_dates:  Optional[list[str]] = None
    history_window: int = 60


@router.post("/correlation")
def correlation(req: CorrelationRequest, token: str = Depends(get_token)):
    """
    How often the registered signals fire together, and whether they agree.

    The question to answer before combining any of them: three signals agreeing
    is only three confirmations if they are measuring different things.
    """
    from backtest.correlation import correlation_report, session_activity

    ids = req.signal_ids or sorted({s.signal_id for s in list_signals()})
    for sid in ids:
        try:
            get_signal(sid)
        except KeyError as e:
            raise HTTPException(404, str(e))

    dates = req.session_dates or recorded_dates(req.symbol)
    if not dates:
        raise HTTPException(
            400, f"No recorded data for {req.symbol}. The recorder must run "
                 f"during market hours before signals can be compared.")

    # Each signal gets the series it would get in a backtest, built once each.
    extras: dict[str, Any] = {}
    notes: list[str] = []
    for sid in ids:
        e, n = _build_extras(req.symbol, sid)
        extras.update(e)
        notes.extend(x for x in n if x not in notes)

    try:
        activities = {
            sid: session_activity(sid, req.symbol, dates, extras=extras,
                                  history_window=req.history_window)
            for sid in ids
        }
    except Exception as e:
        logger.exception("Correlation report failed")
        raise HTTPException(500, f"Correlation report failed: {e}")

    report = correlation_report(activities)
    report["symbol"] = req.symbol
    report["notes"] = notes + report["notes"]
    return report


@router.get("/evaluations/{signal_id}")
def evaluations(signal_id: str, version: Optional[int] = None,
                token: str = Depends(get_token)):
    """
    Fire rate and the distribution of skip reasons.

    The skip reasons are the diagnostic: a signal that never fires because it is
    always warming up is broken, while one that never fires because its
    threshold is never met is merely selective.
    """
    return evaluation_summary(signal_id, version)
