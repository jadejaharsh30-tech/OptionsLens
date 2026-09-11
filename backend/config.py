# optionslens/backend/config.py
# All constants and underlying configs in one place.
import os

RISK_FREE_RATE = 0.065          # 91-day T-bill rate, India (~6.5%)
IV_SOLVER_MAX_ITER = 100
IV_SOLVER_TOL = 1e-6
IV_SOLVER_INITIAL_GUESS = 0.3   # 30% IV starting guess

SNAPSHOT_TIME_IST = "15:20"     # Daily IV snapshot time

# ── Database paths ────────────────────────────────────────────────────────────
# Env-configurable so the Docker volume mount actually persists data. These were
# previously hardcoded while docker-compose.yml set DB_PATH and mounted ./data,
# so nothing read the variable and every rebuild silently discarded the IV
# history and the recorded chain data.
DB_PATH           = os.getenv("DB_PATH",           "optionslens.db")
ALERT_ENGINE_DB   = os.getenv("ALERT_ENGINE_DB",   "oi_engine.db")
MARKET_DATA_DB    = os.getenv("MARKET_DATA_DB",    "market_data.db")

# Fyers symbol strings
# NOTE: Fyers expiry date format is DD-MM-YYYY (e.g. "24-04-2025")
UNDERLYINGS = {
    "NIFTY":     {"symbol": "NSE:NIFTY50-INDEX",   "lot_size": 75,  "strike_step": 50},
    "BANKNIFTY": {"symbol": "NSE:NIFTYBANK-INDEX",  "lot_size": 15,  "strike_step": 100},
    "RELIANCE":  {"symbol": "NSE:RELIANCE-EQ",      "lot_size": 250, "strike_step": 20},
    "TCS":       {"symbol": "NSE:TCS-EQ",           "lot_size": 150, "strike_step": 25},
    "HDFCBANK":  {"symbol": "NSE:HDFCBANK-EQ",      "lot_size": 550, "strike_step": 10},
    "INFY":      {"symbol": "NSE:INFY-EQ",          "lot_size": 300, "strike_step": 20},
    "ICICIBANK": {"symbol": "NSE:ICICIBANK-EQ",     "lot_size": 700, "strike_step": 10},
}

VALID_SYMBOLS = list(UNDERLYINGS.keys())
