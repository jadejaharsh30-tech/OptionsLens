# optionslens/backend/scheduler.py
"""
Daily cron jobs.

Two jobs, split by CAS (the closing auction, live 3 Aug 2026):

  15:10 IST  run_daily_snapshot     — ATM IV for all underlyings, taken during
                                      continuous trading, before the auction
                                      opens at 15:15. Builds the IV history
                                      that /api/ivrank ranks against.
  15:50 IST  run_eod_close_capture  — official closing prices, taken after the
                                      auction settles and derivatives stop.

Splitting them is the point. A single 15:20 job sat inside the auction window,
where F&O-eligible cash stocks have no continuous trading, and recorded a stale
pre-auction print as the day's close.

Token registration: when the user validates their token via /api/auth/validate
it is registered here for the cron jobs. Without one, both skip gracefully.
"""
import logging
from datetime import date
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fyers_client import (
    fetch_expiry_list, fetch_historical_prices, fetch_option_chain,
    fetch_quote, get_fyers,
)
from chain_pricing import implied_forward_for_chain, price_for_iv
from iv_engine import implied_vol_forward
from market_hours import is_trading_day, now_ist
from recorder.store import write_eod_close
from snapshot_store import init_db, write_iv_snapshot, write_atm_iv, write_spot_price
from config import (
    UNDERLYINGS, RISK_FREE_RATE, DB_PATH,
    IV_SNAPSHOT_TIME_IST, EOD_CLOSE_TIME_IST,
)

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

            # NOTE: the closing price is NOT written here. This job runs during
            # continuous trading, so `spot` is an intraday print, not a close.
            # `run_eod_close_capture` records the official close after the
            # auction settles.

            # Use nearest non-expired expiry
            from market_hours import time_to_expiry as days_to_expiry
            exp = next((e for e in expiries if days_to_expiry(e["date"]) > 0), None)
            if exp is None:
                logger.warning(f"All expiries past for {symbol_key}, skipping.")
                continue

            T     = days_to_expiry(exp["date"])
            chain = fetch_option_chain(fyers, symbol_key, exp["expiry"], strike_count=10)

            # Forward-based, matching the live endpoints. IV Rank compares
            # today's IV against this stored history, so the two must be
            # computed the same way or the rank is meaningless.
            forward = implied_forward_for_chain(chain, T, spot)
            if forward is None:
                logger.warning(f"No implied forward for {symbol_key} — skipping.")
                continue

            atm_iv_values = []
            for opt in chain:
                price = price_for_iv(opt)
                if not price:
                    continue
                iv = implied_vol_forward(
                    market_price=price,
                    F=forward, K=opt["strike"], T=T,
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


async def run_eod_close_capture():
    """
    Runs at 15:50 IST, after the closing auction has settled and derivatives
    have stopped trading.

    Takes the official close from the exchange's own daily candle rather than
    sampling a live quote. Under CAS the close for an F&O-eligible stock is the
    auction equilibrium price — it is not any price you can observe by polling
    during the session, so the only correct source is the daily bar.

    Written to two places on purpose:
      - `spot_history` (optionslens.db) — the app's own price series
      - `eod_close`    (market_data.db) — research data, with provenance
    """
    if not _snapshot_token:
        logger.warning("EOD close capture skipped — no token registered.")
        return

    now   = now_ist()
    today = now.date().isoformat()

    if not is_trading_day(now.date()):
        logger.info("EOD close capture skipped — not a trading day.")
        return

    fyers    = get_fyers(_snapshot_token)
    captured = 0

    for symbol_key in UNDERLYINGS:
        try:
            closes = fetch_historical_prices(fyers, symbol_key, days=5)
            if not closes:
                logger.warning(f"No daily candles for {symbol_key} — no close captured.")
                continue

            official_close = closes[-1]
            write_spot_price(DB_PATH, today, symbol_key, official_close)
            write_eod_close(
                session_date = today,
                symbol       = symbol_key,
                close_price  = official_close,
                source       = "fyers_daily_candle",
                captured_at  = now.isoformat(),
            )
            captured += 1
            logger.info(f"EOD close {symbol_key}: {official_close:.2f}")

        except Exception as e:
            logger.error(f"EOD close capture failed for {symbol_key}: {repr(e)}")

    logger.info(f"EOD close capture complete for {today} — {captured} symbols.")


def start_scheduler():
    """
    Starts the APScheduler background jobs.
    Called once at FastAPI app startup via lifespan context manager.
    """
    init_db(DB_PATH)  # Ensure tables exist on startup

    iv_hour, iv_min = (int(x) for x in IV_SNAPSHOT_TIME_IST.split(":"))
    scheduler.add_job(
        run_daily_snapshot,
        CronTrigger(hour=iv_hour, minute=iv_min, timezone="Asia/Kolkata"),
        id="daily_iv_snapshot",
        replace_existing=True,
        misfire_grace_time=300,  # 5 min grace — fires even if slightly delayed
    )

    eod_hour, eod_min = (int(x) for x in EOD_CLOSE_TIME_IST.split(":"))
    scheduler.add_job(
        run_eod_close_capture,
        CronTrigger(hour=eod_hour, minute=eod_min, timezone="Asia/Kolkata"),
        id="eod_close_capture",
        replace_existing=True,
        misfire_grace_time=1800,  # 30 min — the close does not change, so a
                                  # late run is still correct
    )

    scheduler.start()
    logger.info(
        f"APScheduler started. IV snapshot {IV_SNAPSHOT_TIME_IST} IST "
        f"(pre-auction), EOD close {EOD_CLOSE_TIME_IST} IST (post-auction)."
    )
