# optionslens/backend/routers/alert_engine.py
"""
REST endpoints for the OI Alert Engine.

POST   /api/alert-engine/start          → start engine with config
POST   /api/alert-engine/stop           → stop engine
GET    /api/alert-engine/status         → running state + last poll info
GET    /api/alert-engine/alerts         → today's confirmed alerts
GET    /api/alert-engine/chain-snapshot → live chain table data for UI
DELETE /api/alert-engine/alerts/{id}    → suppress a specific alert
"""
import asyncio
import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_token
from fyers_client import get_fyers, fetch_quote
from alert_engine.db import load_alerts_today, suppress_alert
from alert_engine.engine import engine_task
from alert_engine.models import EngineConfig, engine_state

router  = APIRouter(prefix="/api/alert-engine", tags=["alert-engine"])
logger  = logging.getLogger(__name__)

# Module-level handle to the running asyncio task
_engine_task_handle: Optional[asyncio.Task] = None


# ── Request model ─────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    symbols:                list  = ["NIFTY", "BANKNIFTY"]
    poll_interval_sec:      int   = 10
    oi_spike_threshold_pct: float = 500.0
    oi_speed_window_min:    int   = 5
    premium_confirm_polls:  int   = 4
    min_volume_filter:      int   = 100
    strikes_either_side:    int   = 1
    # ── Adaptive threshold ────────────────────────────────────────────────────
    use_adaptive_threshold: bool  = False
    adaptive_percentile:    float = 90.0


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/start")
async def start_engine(req: StartRequest, token: str = Depends(get_token)):
    """
    Start the OI alert engine as an asyncio background task.
    Returns 409 if already running.
    Validates token against Fyers before launching.
    """
    global _engine_task_handle

    if engine_state.running:
        raise HTTPException(status_code=409, detail="Engine is already running.")

    from config import UNDERLYINGS
    invalid = [s for s in req.symbols if s not in UNDERLYINGS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown symbols: {invalid}")

    if not req.symbols:
        raise HTTPException(status_code=400, detail="At least one symbol required.")

    # Quick token validation before starting the long-running task
    try:
        fyers = get_fyers(token)
        fetch_quote(fyers, "NIFTY")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Token validation failed: {e}")

    cfg = EngineConfig(
        symbols                = req.symbols,
        poll_interval_sec      = req.poll_interval_sec,
        oi_spike_threshold_pct = req.oi_spike_threshold_pct,
        oi_speed_window_min    = req.oi_speed_window_min,
        premium_confirm_polls  = req.premium_confirm_polls,
        min_volume_filter      = req.min_volume_filter,
        strikes_either_side    = req.strikes_either_side,
        use_adaptive_threshold = req.use_adaptive_threshold,
        adaptive_percentile    = req.adaptive_percentile,
    )

    _engine_task_handle = asyncio.create_task(engine_task(token, cfg))
    logger.info(
        f"Engine task created — symbols: {cfg.symbols} | "
        f"adaptive: {cfg.use_adaptive_threshold}"
    )

    return {
        "started":                True,
        "symbols":                cfg.symbols,
        "poll_interval_sec":      cfg.poll_interval_sec,
        "oi_spike_threshold_pct": cfg.oi_spike_threshold_pct,
        "use_adaptive_threshold": cfg.use_adaptive_threshold,
        "adaptive_percentile":    cfg.adaptive_percentile,
        "strikes_either_side":    cfg.strikes_either_side,
    }


@router.post("/stop")
async def stop_engine(token: str = Depends(get_token)):
    """Stop the engine gracefully. Sets running=False; task exits on next iteration."""
    global _engine_task_handle

    if not engine_state.running:
        return {"stopped": False, "reason": "Engine was not running."}

    engine_state.running = False

    if _engine_task_handle and not _engine_task_handle.done():
        _engine_task_handle.cancel()
        try:
            await _engine_task_handle
        except asyncio.CancelledError:
            pass
        _engine_task_handle = None

    logger.info("Engine stopped via API.")
    return {"stopped": True}


@router.get("/status")
def get_status(token: str = Depends(get_token)):
    """
    Returns current engine state for the UI status indicator.
    Frontend polls this every 5 seconds.
    """
    pending_summary = {}
    for symbol, spikes in engine_state.pending_spikes.items():
        pending_summary[symbol] = [
            {
                "strike":       k[0],
                "option_type":  k[1],
                "polls_waited": v.polls_waited,
                "oi_pct":       round(v.oi_pct, 1),
                "ltp_at_spike": v.ltp_at_spike,
            }
            for k, v in spikes.items()
        ]

    config_out = None
    if engine_state.config:
        cfg = engine_state.config
        config_out = {
            "oi_spike_threshold_pct": cfg.oi_spike_threshold_pct,
            "premium_confirm_polls":  cfg.premium_confirm_polls,
            "strikes_either_side":    cfg.strikes_either_side,
            "poll_interval_sec":      cfg.poll_interval_sec,
            "min_volume_filter":      cfg.min_volume_filter,
            "use_adaptive_threshold": cfg.use_adaptive_threshold,
            "adaptive_percentile":    cfg.adaptive_percentile,
        }

    return {
        "running":             engine_state.running,
        "started_at":          engine_state.started_at.isoformat()
                               if engine_state.started_at else None,
        "last_poll_at":        engine_state.last_poll_at.isoformat()
                               if engine_state.last_poll_at else None,
        "last_poll_status":    engine_state.last_poll_status,
        "last_error":          engine_state.last_error,
        "alert_count_session": engine_state.alert_count_session,
        "active_symbols":      engine_state.config.symbols
                               if engine_state.config else [],
        "pending_spikes":      pending_summary,
        "config":              config_out,
    }


@router.get("/alerts")
def get_alerts(token: str = Depends(get_token)):
    """Returns today's confirmed (unsuppressed) alerts, newest first."""
    session_date = date.today().isoformat()
    alerts = load_alerts_today(session_date)
    return {
        "session_date": session_date,
        "alerts":       alerts,
        "count":        len(alerts),
    }


@router.get("/chain-snapshot")
def get_chain_snapshot(token: str = Depends(get_token)):
    """Returns the latest chain snapshot for all monitored symbols."""
    return {"snapshots": engine_state.chain_snapshot}


@router.delete("/alerts/{alert_id}")
def suppress_alert_endpoint(alert_id: int, token: str = Depends(get_token)):
    """Suppress a specific alert from the log."""
    try:
        suppress_alert(alert_id)
        return {"suppressed": True, "alert_id": alert_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
