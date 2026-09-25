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


def _build_extras(symbol: str) -> tuple[dict, list[str]]:
    """
    Assemble the history a signal cannot reach on its own.

    A signal sees ChainSnapshots only, which is what makes live and backtest
    identical — but VRP ranks against two years of daily readings, and vol
    labelling needs the implied level at entry. Both arrive here, built by
    `vrp.load_vrp_history` so the API and any script compute them identically.

    Returns the extras plus notes, because a signal that silently skips every
    bar for want of a series is the least debuggable outcome there is.
    """
    from vrp import load_vrp_history, summarise

    notes: list[str] = []
    try:
        series = load_vrp_history(DB_PATH, symbol)
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"VRP history unavailable for {symbol}: {e!r}")
        return {}, [f"VRP history could not be loaded: {e}"]

    if not series:
        notes.append(
            f"No paired IV/RV history for {symbol}, so vrp will skip every bar "
            f"and vol outcomes cannot be scored. Run the bhavcopy import "
            f"(docs/BHAVCOPY.md) to populate atm_iv_history and spot_history.")
        return {}, notes

    info = summarise(series)
    notes.append(
        f"VRP history: {info['observations']} paired observations, "
        f"{info['first_date']} to {info['last_date']}.")
    return ({"vrp_series": series,
             "iv_series": [{"date": r["date"], "iv": r["iv"]} for r in series]},
            notes)


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

    extras, extra_notes = _build_extras(req.symbol)

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
