# optionslens/backend/recorder/service.py
"""
The chain recorder: an always-on async loop that snapshots full option chains
to the append-only store.

Two design choices worth keeping:

1. Blocking Fyers SDK calls and SQLite writes run via `asyncio.to_thread`.
   The alert engine calls its blocking tick directly on the event loop, which
   freezes every other API request for the duration of the poll. The recorder
   must never do that — it runs all day.

2. Polls are aligned to interval boundaries (see `seconds_until_next_tick`), so
   snapshots across symbols share timestamps and resample into bars cleanly
   without interpolation.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import UNDERLYINGS
from futures import front_month_expiry, fyers_future_symbol
from fyers_client import fetch_option_chain, fetch_expiry_list, fetch_quotes, get_fyers
from market_hours import (
    SessionPhase, get_session_phase, is_recording_window,
    now_ist, seconds_until_next_tick,
)
from recorder.models import ChainRow, ChainSnapshot
from recorder.store import MARKET_DATA_DB, init_db, write_snapshot

logger = logging.getLogger(__name__)


@dataclass
class RecorderConfig:
    """Capture settings. Wider is better — you cannot widen retroactively."""
    symbols:       list[str] = field(default_factory=lambda: ["NIFTY", "BANKNIFTY"])
    interval_sec:  int = 60           # 1-minute bars
    strike_count:  int = 15           # strikes either side of ATM
    expiry_depth:  int = 1            # how many nearest expiries to record
    db_path:       str = MARKET_DATA_DB


@dataclass
class RecorderState:
    """Live status, read by the API. Single-threaded asyncio — no locks needed."""
    running:            bool = False
    started_at:         Optional[datetime] = None
    last_write_at:      Optional[datetime] = None
    last_phase:         Optional[str] = None
    rows_written:       int = 0
    snapshots_written:  int = 0
    consecutive_errors: int = 0
    last_error:         Optional[str] = None
    config:             Optional[RecorderConfig] = None
    # Futures are best-effort, so their health is tracked separately: a chain
    # recorded without its future is still worth keeping, but a recorder that
    # has never once captured a future has a symbol-format problem.
    futures_captured:   int = 0
    futures_missed:     int = 0
    last_futures_symbol: dict = field(default_factory=dict)
    futures_warned:     set = field(default_factory=set)


recorder_state = RecorderState()


# ── Capture (blocking — always called via asyncio.to_thread) ──────────────────

def _capture_symbol(fyers, symbol: str, cfg: RecorderConfig) -> list[ChainSnapshot]:
    """
    Build snapshots for one symbol. Blocking: network + CPU only, no DB writes.

    Timestamp is taken once per symbol and shared by every expiry so that rows
    from one poll are joinable.
    """
    now = now_ist()
    ts = now.replace(microsecond=0).isoformat()
    phase = get_session_phase(now)

    # Expiries first: the front-month future is found from the option calendar,
    # and knowing it lets spot and futures share one quote call.
    expiries = fetch_expiry_list(fyers, symbol)
    if not expiries:
        return []

    spot, fut_price, fut_expiry = _quote_spot_and_future(fyers, symbol, expiries, now)

    snapshots: list[ChainSnapshot] = []
    for exp in expiries[:cfg.expiry_depth]:
        chain = fetch_option_chain(fyers, symbol, exp["expiry"],
                                   strike_count=cfg.strike_count)
        if not chain:
            continue

        rows = tuple(
            ChainRow(
                strike        = r["strike"],
                option_type   = r["option_type"],
                oi            = r.get("oi", 0) or 0,
                oi_change     = r.get("oi_change", 0) or 0,
                oi_change_pct = r.get("oi_change_pct", 0) or 0,
                prev_oi       = r.get("prev_oi", 0) or 0,
                ltp           = r.get("ltp", 0) or 0,
                bid           = r.get("bid", 0) or 0,
                ask           = r.get("ask", 0) or 0,
                volume        = r.get("volume", 0) or 0,
            )
            for r in chain
        )

        snapshots.append(ChainSnapshot(
            ts            = ts,
            session_date  = now.date().isoformat(),
            session_phase = phase,
            symbol        = symbol,
            expiry_date   = exp["date"],
            expiry_epoch  = exp["expiry"],
            spot          = spot,
            futures       = fut_price,
            futures_expiry = fut_expiry,
            rows          = rows,
        ))

    return snapshots


def _quote_spot_and_future(fyers, symbol: str, expiries: list[dict], now,
                           ) -> tuple[float, Optional[float], Optional[str]]:
    """
    Spot and front-month future in one quote call.

    Spot failing is fatal for the poll, exactly as before — there is no chain
    worth recording without it. The future failing is not: the snapshot is
    still written with `futures=None`, and the miss is counted so a wrong
    symbol format is visible on the status endpoint instead of silently
    producing a column of nulls for months.
    """
    spot_sym = UNDERLYINGS[symbol]["symbol"]
    expiry = front_month_expiry([e.get("date") for e in expiries], now=now)
    fut_sym = fyers_future_symbol(symbol, expiry) if expiry else None

    quotes = fetch_quotes(fyers, [spot_sym] + ([fut_sym] if fut_sym else []))
    if spot_sym not in quotes:
        raise ValueError(f"No spot quote for {symbol} ({spot_sym})")

    fut_price = quotes.get(fut_sym) if fut_sym else None
    _count_future(symbol, fut_sym, fut_price)
    fut_expiry = expiry.strftime("%d-%m-%Y") if (expiry and fut_price) else None
    return quotes[spot_sym], fut_price, fut_expiry


def _count_future(symbol: str, fut_sym: Optional[str], price: Optional[float]) -> None:
    """Tally futures captures and warn once per symbol per run on a miss."""
    recorder_state.last_futures_symbol[symbol] = fut_sym
    if price is not None:
        recorder_state.futures_captured += 1
        return
    recorder_state.futures_missed += 1
    if symbol not in recorder_state.futures_warned:
        recorder_state.futures_warned.add(symbol)
        logger.warning(
            f"Recorder: no futures quote for {symbol} (tried {fut_sym!r}). Chains "
            f"are still recorded, with futures=None. If this repeats for every "
            f"symbol the Fyers futures symbol format in futures.py is wrong.")


def _persist(snapshots: list[ChainSnapshot], db_path: str) -> int:
    """Blocking DB writes, batched per poll."""
    return sum(write_snapshot(s, db_path) for s in snapshots)


# ── Main loop ─────────────────────────────────────────────────────────────────

async def recorder_task(token: str, cfg: RecorderConfig):
    """
    Run until `recorder_state.running` goes False.

    Never raises out of the loop: a recorder that dies on one bad poll is worse
    than useless, because the data loss is silent and permanent.
    """
    await asyncio.to_thread(init_db, cfg.db_path)

    fyers = get_fyers(token)
    recorder_state.running    = True
    recorder_state.started_at = now_ist()
    recorder_state.config     = cfg
    recorder_state.last_error = None
    # Warn afresh each run: a restart after fixing the symbol format should
    # report whether the fix worked, not stay silent from the last run.
    recorder_state.futures_warned = set()

    logger.info(
        f"Recorder started — symbols: {cfg.symbols} | "
        f"every {cfg.interval_sec}s | ±{cfg.strike_count} strikes | "
        f"db: {cfg.db_path}"
    )

    while recorder_state.running:
        try:
            phase = get_session_phase()
            recorder_state.last_phase = phase.value

            if not is_recording_window():
                # Outside 09:15-15:40 there is nothing to capture. Poll slowly
                # so the session rolls into the next trading day unattended.
                await asyncio.sleep(min(300, cfg.interval_sec * 5))
                continue

            total_rows = 0
            for symbol in cfg.symbols:
                if not recorder_state.running:
                    break
                if symbol not in UNDERLYINGS:
                    logger.warning(f"Recorder: unknown symbol {symbol}, skipping.")
                    continue
                try:
                    snapshots = await asyncio.to_thread(
                        _capture_symbol, fyers, symbol, cfg
                    )
                    if snapshots:
                        written = await asyncio.to_thread(
                            _persist, snapshots, cfg.db_path
                        )
                        total_rows += written
                        recorder_state.snapshots_written += len(snapshots)
                except Exception as sym_err:
                    # One bad symbol must not cost us the others this minute.
                    logger.error(f"Recorder capture failed [{symbol}]: {repr(sym_err)}")

            if total_rows:
                recorder_state.rows_written  += total_rows
                recorder_state.last_write_at  = now_ist()

            recorder_state.consecutive_errors = 0
            recorder_state.last_error = None

        except asyncio.CancelledError:
            logger.info("Recorder task cancelled.")
            break
        except Exception as e:
            recorder_state.consecutive_errors += 1
            recorder_state.last_error = repr(e)
            logger.error(f"Recorder loop error: {repr(e)}")

        await asyncio.sleep(seconds_until_next_tick(cfg.interval_sec))

    recorder_state.running = False
    logger.info(
        f"Recorder stopped. Session totals — "
        f"{recorder_state.snapshots_written} snapshots, "
        f"{recorder_state.rows_written} rows."
    )
