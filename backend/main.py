# optionslens/backend/main.py
"""
OptionsLens API — FastAPI application entry point.
All routers registered here. Scheduler started on app startup.

Run with:
    uvicorn main:app --reload --port 8000

API docs (auto-generated):
    http://localhost:8000/docs
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from scheduler import start_scheduler, register_token
from auth import get_token
from fyers_client import fetch_quote, get_fyers
from alert_engine.db import init_db as init_alert_engine_db
from recorder.store import init_db as init_market_data_db
from signals.store import init_db as init_signal_store
from trading.store import init_db as init_trade_store

# ── Routers ───────────────────────────────────────────────────────────────────
from routers.expiries     import router as expiries_router
from routers.chain        import router as chain_router
from routers.surface      import router as surface_router
from routers.oi           import router as oi_router
from routers.ivrank       import router as ivrank_router
from routers.position_lab import router as position_lab_router
from routers.alert_engine import router as alert_engine_router
from routers.recorder     import router as recorder_router, start_recorder
from recorder.service import recorder_state
from routers.backtest     import router as backtest_router
from routers.trades       import router as trades_router
from routers.notifications import router as notify_router

# Importing the signal library registers every signal. Must happen before the
# registry is queried by /api/backtest/signals.
import signals.library  # noqa: F401

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Lifespan: start scheduler on app boot ────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    init_alert_engine_db()  # Ensure alert-engine tables exist before the frontend polls /alerts
    init_market_data_db()   # Chain recorder store — append-only research data
    init_signal_store()     # Signal evaluation log (same DB file)
    init_trade_store()      # Trade lifecycle + journal
    logger.info("OptionsLens API started.")
    yield
    logger.info("OptionsLens API shutting down.")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="OptionsLens API",
    version="1.0.0",
    description="NSE Options Intelligence Dashboard — IV surface, OI analysis, Position Lab",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Register routers ──────────────────────────────────────────────────────────
app.include_router(expiries_router)
app.include_router(chain_router)
app.include_router(surface_router)
app.include_router(oi_router)
app.include_router(ivrank_router)
app.include_router(position_lab_router)
app.include_router(alert_engine_router)
app.include_router(recorder_router)
app.include_router(backtest_router)
app.include_router(trades_router)
app.include_router(notify_router)


# ── Core endpoints ────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
def health():
    """Server health check — no auth required."""
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/auth/validate", tags=["auth"])
async def validate_token(token: str = Depends(get_token)):
    """
    Validates a Fyers access token by fetching a live Nifty quote.
    Also registers the token for the daily 15:10 IST IV snapshot job.
    Call this once each morning after pasting your token.

    Async on purpose. Auto-starting the recorder creates an asyncio task, which
    needs the running event loop; as a plain `def` this endpoint ran in a worker
    thread with no loop, so every validation failed with "no running event
    loop" and was reported as an invalid token. The blocking Fyers call goes
    through to_thread so it still does not stall the loop.
    """
    try:
        fyers = get_fyers(token)
        nifty_ltp = await asyncio.to_thread(fetch_quote, fyers, "NIFTY")
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))

    register_token(token)

    # Auto-start the chain recorder. Intraday per-strike OI cannot be bought
    # back retroactively, so recording must not depend on the user remembering
    # to press a button each morning. A recorder failure is logged, not turned
    # into a 401: the token is valid, and the dashboard should still open.
    try:
        recorder_started = start_recorder(token)
    except Exception as e:
        logger.error(f"Recorder auto-start failed: {e!r}")
        recorder_started = False

    logger.info(f"Token validated. NIFTY LTP: {nifty_ltp}")
    return {
        "valid":            True,
        "nifty_ltp":        nifty_ltp,
        "recorder_started": recorder_started,
        "message":          "Token valid. Registered for daily IV snapshot at 15:10 IST. "
                            + ("Chain recorder started." if recorder_started else
                               "Chain recorder already running." if recorder_state.running else
                               "Chain recorder not started; see backend log."),
    }


@app.get("/api/symbols", tags=["meta"])
def list_symbols():
    """Returns all configured underlyings with lot sizes and strike steps."""
    from config import UNDERLYINGS
    from lot_sizes import lot_size_for
    return {
        "symbols": [
            {
                "key":          k,
                "fyers_symbol": v["symbol"],
                "lot_size":     lot_size_for(k),
                "strike_step":  v["strike_step"],
            }
            for k, v in UNDERLYINGS.items()
        ]
    }
