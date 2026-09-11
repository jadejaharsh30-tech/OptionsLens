# optionslens/backend/scheduler.py
"""
Daily IV snapshot job — runs at 15:20 IST every market day.
Snapshots ATM IV and spot price for all configured underlyings, writes to SQLite.
This builds the historical IV store used by /api/ivrank (IV Rank + Realized Vol).

Token registration: when the user validates their token via
/api/auth/validate, it is registered here for the cron job to use.
If no token is registered by 15:20, the job is skipped gracefully.
"""
import logging
from datetime import date
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fyers_client import fetch_expiry_list, fetch_option_chain, fetch_quote, get_fyers
from iv_engine import implied_volatility
from snapshot_store import init_db, write_iv_snapshot, write_atm_iv, write_spot_price
from config import UNDERLYINGS, RISK_FREE_RATE, DB_PATH

logger    = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

# Token stored here when /api/auth/validate is called successfully.
# In-memory only — cleared on server restart.
_snapshot_token: str | None = None


def register_token(token: str):
    """
    Called by /api/auth/validate.
    Stores token for the daily snapshot job.
    Overwrites any previously registered token.
    """
    global _snapshot_token
    _snapshot_token = token
    logger.info("Snapshot token registered for daily job.")


async def run_daily_snapshot():
    """
    Runs at 15:20 IST. Fetches live IV + spot for all underlyings and persists to SQLite.
    Errors per-symbol are logged but never crash the server.
    """
    if not _snapshot_token:
        logger.warning("Daily IV snapshot skipped — no token registered. "
                       "Call /api/auth/validate before 15:20 IST.")
        return

    today = date.today().isoformat()
    fyers = get_fyers(_snapshot_token)
    logger.info(f"Daily IV snapshot starting for {today}...")

    for symbol_key in UNDERLYINGS:
        try:
            spot     = fetch_quote(fyers, symbol_key)
            expiries = fetch_expiry_list(fyers, symbol_key)

            if not expiries:
                logger.warning(f"No expiries for {symbol_key}, skipping.")
                continue

            # Persist today's closing spot price for realized vol computation
            write_spot_price(DB_PATH, today, symbol_key, spot)

            # Use nearest non-expired expiry
            from market_hours import time_to_expiry as days_to_expiry
            exp = next((e for e in expiries if days_to_expiry(e["date"]) > 0), None)
            if exp is None:
                logger.warning(f"All expiries past for {symbol_key}, skipping.")
                continue

            T     = days_to_expiry(exp["date"])
            chain = fetch_option_chain(fyers, symbol_key, exp["expiry"], strike_count=10)

            atm_iv_values = []
            for opt in chain:
                if opt["ltp"] <= 0:
                    continue
                iv = implied_volatility(
                    market_price=opt["ltp"],
                    S=spot, K=opt["strike"], T=T,
                    r=RISK_FREE_RATE,
                    option_type=opt["option_type"],
                )
                if iv is None:
                    continue

                # Write every strike's IV snapshot
                write_iv_snapshot(
                    db_path=DB_PATH,
                    snapshot_date=today,
                    symbol=symbol_key,
                    expiry_date=exp["date"],
                    strike=opt["strike"],
                    option_type=opt["option_type"],
                    iv=iv,
                    ltp=opt["ltp"],
                    oi=opt["oi"],
                )

                # Collect near-ATM IVs (within 2% of spot) for ATM average
                if abs(opt["strike"] - spot) / spot <= 0.02:
                    atm_iv_values.append(iv)

            # Write ATM IV to history table (used for IV Rank)
            if atm_iv_values:
                atm_iv = sum(atm_iv_values) / len(atm_iv_values)
                write_atm_iv(DB_PATH, today, symbol_key, exp["date"], atm_iv)
                logger.info(
                    f"Snapshot saved: {symbol_key} "
                    f"ATM IV = {atm_iv * 100:.1f}% "
                    f"(spot={spot:.0f}, expiry={exp['date']})"
                )
            else:
                logger.warning(f"No ATM IV computed for {symbol_key} — "
                               f"all near-ATM options had zero LTP or unsolvable IV.")

        except Exception as e:
            logger.error(f"Snapshot failed for {symbol_key}: {repr(e)}")

    logger.info(f"Daily IV snapshot complete for {today}.")


def start_scheduler():
    """
    Starts the APScheduler background job.
    Called once at FastAPI app startup via lifespan context manager.
    """
    init_db(DB_PATH)  # Ensure tables exist on startup

    scheduler.add_job(
        run_daily_snapshot,
        CronTrigger(hour=15, minute=20, timezone="Asia/Kolkata"),
        id="daily_iv_snapshot",
        replace_existing=True,
        misfire_grace_time=300,  # 5 min grace — fires even if slightly delayed
    )
    scheduler.start()
    logger.info("APScheduler started. Daily IV snapshot scheduled at 15:20 IST.")
