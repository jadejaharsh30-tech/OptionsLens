# optionslens/backend/routers/recorder.py
"""
Control and observability for the chain recorder.

POST /api/recorder/start    → start recording (auto-started on token validation)
POST /api/recorder/stop     → stop recording
GET  /api/recorder/status   → running state, session counters, last error
GET  /api/recorder/coverage → dataset summary: days and snapshots held per symbol
GET  /api/recorder/dates    → session dates available for backtesting

The coverage endpoint exists so a silently dead recorder becomes visible. The
failure mode that matters is not a crash — it is three weeks of empty days
nobody noticed.
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_token
from market_hours import get_session_phase, now_ist
from recorder.quality import day_coverage, quality_report
from recorder.service import RecorderConfig, recorder_state, recorder_task
from recorder.store import coverage_stats, recorded_dates

router = APIRouter(prefix="/api/recorder", tags=["recorder"])
logger = logging.getLogger(__name__)

_recorder_handle: Optional[asyncio.Task] = None


class StartRecorderRequest(BaseModel):
    symbols:      list[str] = ["NIFTY", "BANKNIFTY"]
    interval_sec: int = 60
    strike_count: int = 15
    expiry_depth: int = 1


def start_recorder(token: str, cfg: Optional[RecorderConfig] = None) -> bool:
    """
    Launch the recorder if it is not already running.

    Importable so `/api/auth/validate` can auto-start it — relying on the user
    to remember a button every morning is how trading days get lost.
    Returns True if this call started it.
    """
    global _recorder_handle

    if recorder_state.running:
        return False

    cfg = cfg or RecorderConfig()
    _recorder_handle = asyncio.create_task(recorder_task(token, cfg))
    logger.info(f"Recorder task created for {cfg.symbols}.")
    return True


@router.post("/start")
async def start(req: StartRecorderRequest, token: str = Depends(get_token)):
    """Start recording with explicit settings."""
    if recorder_state.running:
        raise HTTPException(409, "Recorder is already running.")

    cfg = RecorderConfig(
        symbols      = req.symbols,
        interval_sec = req.interval_sec,
        strike_count = req.strike_count,
        expiry_depth = req.expiry_depth,
    )
    start_recorder(token, cfg)
    return {"started": True, "symbols": cfg.symbols,
            "interval_sec": cfg.interval_sec, "strike_count": cfg.strike_count}


@router.post("/stop")
async def stop(token: str = Depends(get_token)):
    """Stop recording. Data already written is never removed."""
    global _recorder_handle

    if not recorder_state.running:
        return {"stopped": False, "reason": "Recorder was not running."}

    recorder_state.running = False
    if _recorder_handle and not _recorder_handle.done():
        _recorder_handle.cancel()
        try:
            await _recorder_handle
        except asyncio.CancelledError:
            pass
        _recorder_handle = None

    return {"stopped": True,
            "snapshots_written": recorder_state.snapshots_written,
            "rows_written":      recorder_state.rows_written}


@router.get("/status")
def status(token: str = Depends(get_token)):
    """Current recorder state plus the live session phase."""
    cfg = recorder_state.config
    return {
        "running":            recorder_state.running,
        "session_phase":      get_session_phase().value,
        "server_time_ist":    now_ist().isoformat(),
        "started_at":         recorder_state.started_at.isoformat()
                              if recorder_state.started_at else None,
        "last_write_at":      recorder_state.last_write_at.isoformat()
                              if recorder_state.last_write_at else None,
        "rows_written":       recorder_state.rows_written,
        "snapshots_written":  recorder_state.snapshots_written,
        "consecutive_errors": recorder_state.consecutive_errors,
        "last_error":         recorder_state.last_error,
        "config": {
            "symbols":      cfg.symbols,
            "interval_sec": cfg.interval_sec,
            "strike_count": cfg.strike_count,
            "expiry_depth": cfg.expiry_depth,
        } if cfg else None,
    }


@router.get("/coverage")
def coverage(token: str = Depends(get_token)):
    """How much research data we actually hold, per symbol."""
    return coverage_stats()


@router.get("/dates")
def dates(symbol: Optional[str] = None, token: str = Depends(get_token)):
    """Session dates available for replay/backtesting."""
    d = recorded_dates(symbol)
    return {"symbol": symbol, "count": len(d), "dates": d}


@router.get("/quality")
def quality(symbols: Optional[str] = None, interval_sec: int = 60,
            token: str = Depends(get_token)):
    """
    Data-quality report: completeness per session, gaps, missing trading days.

    `symbols` is an optional comma-separated filter.
    """
    symbol_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None
    return quality_report(symbol_list, interval_sec)


@router.get("/quality/{symbol}/{session_date}")
def quality_day(symbol: str, session_date: str, interval_sec: int = 60,
                token: str = Depends(get_token)):
    """Coverage detail for one symbol-day, including every gap found."""
    return day_coverage(symbol.upper(), session_date, interval_sec)
