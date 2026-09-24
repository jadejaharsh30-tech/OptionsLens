# optionslens/backend/config.py
# All constants and underlying configs in one place.
import os

RISK_FREE_RATE = 0.065          # 91-day T-bill rate, India (~6.5%)
IV_SOLVER_MAX_ITER = 100
IV_SOLVER_TOL = 1e-6
IV_SOLVER_INITIAL_GUESS = 0.3   # 30% IV starting guess

# ── Daily job times (IST) ─────────────────────────────────────────────────────
# The IV snapshot runs at 15:10, inside continuous trading and before the CAS
# auction opens at 15:15. It used to run at 15:20, which under CAS lands in the
# middle of the auction when F&O-eligible cash stocks have no continuous
# trading and any quoted price is a stale pre-auction print.
IV_SNAPSHOT_TIME_IST = "15:10"
# The official close only exists after the auction settles, so closing prices
# are captured separately once derivatives have stopped trading.
EOD_CLOSE_TIME_IST   = "15:50"

SNAPSHOT_TIME_IST = IV_SNAPSHOT_TIME_IST   # deprecated alias

# ── Database paths ────────────────────────────────────────────────────────────
# Env-configurable so the Docker volume mount actually persists data. These were
# previously hardcoded while docker-compose.yml set DB_PATH and mounted ./data,
# so nothing read the variable and every rebuild silently discarded the IV
# history and the recorded chain data.
DB_PATH           = os.getenv("DB_PATH",           "optionslens.db")
ALERT_ENGINE_DB   = os.getenv("ALERT_ENGINE_DB",   "oi_engine.db")
MARKET_DATA_DB    = os.getenv("MARKET_DATA_DB",    "market_data.db")
# Exchange EOD option history (bhavcopy/). Written by bhavcopy.download, which
# usually runs on a home connection because NSE blocks datacenter ranges, then
# read by bhavcopy.importer. Separate file so the ~1 MB/day archive never bloats
# the app database.
NSE_EOD_DB        = os.getenv("NSE_EOD_DB",        "nse_options_eod.db")

# Fyers symbol strings
# NOTE: Fyers expiry date format is DD-MM-YYYY (e.g. "24-04-2025")
#
# lot_size here is only a FALLBACK. Read lot sizes through lot_sizes.lot_size_for,
# which uses the per-contract sizes NSE publishes (loaded by bhavcopy.importer).
# NSE revises them several times a year, and bonus issues and splits change them
# too. All entries except ICICIBANK were checked against exchange files for
# September 2026 contracts; every stock entry had drifted except that one.
UNDERLYINGS = {
    "NIFTY":     {"symbol": "NSE:NIFTY50-INDEX",   "lot_size": 65,  "strike_step": 50},
    "BANKNIFTY": {"symbol": "NSE:NIFTYBANK-INDEX",  "lot_size": 30,  "strike_step": 100},
    "RELIANCE":  {"symbol": "NSE:RELIANCE-EQ",      "lot_size": 500, "strike_step": 20},
    "TCS":       {"symbol": "NSE:TCS-EQ",           "lot_size": 225, "strike_step": 25},
    "HDFCBANK":  {"symbol": "NSE:HDFCBANK-EQ",      "lot_size": 650, "strike_step": 10},
    "INFY":      {"symbol": "NSE:INFY-EQ",          "lot_size": 400, "strike_step": 20},
    "ICICIBANK": {"symbol": "NSE:ICICIBANK-EQ",     "lot_size": 700, "strike_step": 10},
}

VALID_SYMBOLS = list(UNDERLYINGS.keys())
