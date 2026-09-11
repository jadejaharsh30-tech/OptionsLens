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
from fyers_client import fetch_option_chain, fetch_expiry_list, fetch_quote, get_fyers
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

    spot = fetch_quote(fyers, symbol)
    expiries = fetch_expiry_list(fyers, symbol)
    if not expiries:
        return []

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
            futures       = None,   # TODO(roadmap 15): needs Fyers futures symbol format
            rows          = rows,
        ))

    return snapshots


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
