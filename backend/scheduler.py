# optionslens/backend/scheduler.py
"""
Daily cron jobs.

Two jobs, split by CAS (the closing auction, live 3 Aug 2026):

  15:10 IST  run_daily_snapshot     — ATM IV for the expiries either side of
                                      30 days, taken during continuous trading,
                                      before the auction opens at 15:15. Builds
                                      the 30-day IV history /api/ivrank ranks
                                      against.
  15:50 IST  run_eod_close_capture  — official closing prices, taken after the
                                      auction settles and derivatives stop.

Splitting them is the point. A single 15:20 job sat inside the auction window,
where F&O-eligible cash stocks have no continuous trading, and recorded a stale
pre-auction print as the day's close.

Token registration: when the user validates their token via /api/auth/validate
it is registered here for the cron jobs. Without one, both skip gracefully.
"""
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fyers_client import (
    fetch_expiry_list, fetch_historical_prices, fetch_quote, get_fyers,
)
from chain_pricing import implied_forward_for_chain, price_for_iv
from iv_engine import implied_vol_forward
from live_iv import live_cm_atm_iv
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
    Runs at 15:10 IST. Fetches live IV for all underlyings and persists to SQLite.
    Errors per-symbol are logged but never crash the server.
    """
    if not _snapshot_token:
        logger.warning("Daily IV snapshot skipped — no token registered. "
                       "Call /api/auth/validate before 15:10 IST.")
        return

    # IST date, not date.today(): on a UTC host the two differ after 18:30 IST.
    today = now_ist().date().isoformat()
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

            # The expiries either side of 30 days, not just the nearest one:
            # IV Rank ranks 30-day constant-maturity IV, and a history holding
            # only the front expiry cannot be interpolated to 30 days.
            term = live_cm_atm_iv(fyers, symbol_key, spot, expiries,
                                  strike_count=10)
            if not term.expiries:
                logger.warning(f"No expiries bracket 30 days for {symbol_key}, skipping.")
                continue

            for reading in term.expiries:
                _write_strike_snapshots(today, symbol_key, spot, reading)
                if reading.atm_iv is None:
                    logger.warning(f"No ATM IV for {symbol_key} {reading.expiry_date}.")
                    continue
                write_atm_iv(DB_PATH, today, symbol_key, reading.expiry_date,
                             reading.atm_iv)

            if term.cm_iv is not None:
                logger.info(
                    f"Snapshot saved: {symbol_key} 30d ATM IV = {term.cm_iv * 100:.1f}% "
                    f"(spot={spot:.0f}, expiries={', '.join(term.expiries_used)})"
                )
            else:
                logger.warning(f"No 30-day IV for {symbol_key} today; per-expiry "
                               f"rows written where they solved.")

        except Exception as e:
            logger.error(f"Snapshot failed for {symbol_key}: {repr(e)}")

    logger.info(f"Daily IV snapshot complete for {today}.")


def _write_strike_snapshots(today: str, symbol_key: str, spot: float, reading) -> None:
    """Per-strike IV for one fetched chain, into iv_snapshots."""
    T = reading.tenor_days / 365.0
    forward = implied_forward_for_chain(reading.chain, T, spot)
    if forward is None:
        return
    for opt in reading.chain:
        price = price_for_iv(opt)
        if not price:
            continue
        iv = implied_vol_forward(
            market_price=price, F=forward, K=opt["strike"], T=T,
            r=RISK_FREE_RATE, option_type=opt["option_type"],
        )
        if iv is None:
            continue
        write_iv_snapshot(
            db_path=DB_PATH, snapshot_date=today, symbol=symbol_key,
            expiry_date=reading.expiry_date, strike=opt["strike"],
            option_type=opt["option_type"], iv=iv, ltp=opt["ltp"], oi=opt["oi"],
        )


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

    # Starting twice binds jobs to whichever loop ran first, which is dead by
    # the time the second caller arrives. Production starts the app once, but a
    # test harness (or a reload) can call this repeatedly.
    if scheduler.running:
        logger.info("Scheduler already running; leaving it alone.")
        return

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
