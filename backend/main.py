# optionslens/backend/main.py
"""
OptionsLens API — FastAPI application entry point.
All routers registered here. Scheduler started on app startup.

Run with:
    uvicorn main:app --reload --port 8000

API docs (auto-generated):
    http://localhost:8000/docs
"""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from scheduler import start_scheduler, register_token
from auth import get_token
from fyers_client import fetch_quote, get_fyers
from alert_engine.db import init_db as init_alert_engine_db

# ── Routers ───────────────────────────────────────────────────────────────────
from routers.expiries     import router as expiries_router
from routers.chain        import router as chain_router
from routers.surface      import router as surface_router
from routers.oi           import router as oi_router
from routers.ivrank       import router as ivrank_router
from routers.position_lab import router as position_lab_router
from routers.alert_engine import router as alert_engine_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Lifespan: start scheduler on app boot ────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    init_alert_engine_db()  # Ensure alert-engine tables exist before the frontend polls /alerts
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


# ── Core endpoints ────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
def health():
    """Server health check — no auth required."""
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/auth/validate", tags=["auth"])
def validate_token(token: str = Depends(get_token)):
    """
    Validates a Fyers access token by fetching a live Nifty quote.
    Also registers the token for the daily 15:20 IST IV snapshot job.
    Call this once each morning after pasting your token.
    """
    try:
        fyers = get_fyers(token)
        nifty_ltp = fetch_quote(fyers, "NIFTY")
        register_token(token)
        logger.info(f"Token validated. NIFTY LTP: {nifty_ltp}")
        return {
            "valid":     True,
            "nifty_ltp": nifty_ltp,
            "message":   "Token valid. Registered for daily IV snapshot at 15:20 IST.",
        }
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/symbols", tags=["meta"])
def list_symbols():
    """Returns all configured underlyings with lot sizes and strike steps."""
    from config import UNDERLYINGS
    return {
        "symbols": [
            {
                "key":          k,
                "fyers_symbol": v["symbol"],
                "lot_size":     v["lot_size"],
                "strike_step":  v["strike_step"],
            }
            for k, v in UNDERLYINGS.items()
        ]
    }
