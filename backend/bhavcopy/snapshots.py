# optionslens/backend/bhavcopy/snapshots.py
"""
Exchange end-of-day rows, rebuilt as `ChainSnapshot`s so daily-horizon signals
can be replayed through the existing backtester.

Why an adapter rather than a second backtester: `ChainSnapshot` is the contract
that makes live and replay the same code path. Anything that can be expressed
as one gets the null comparison, the cost model and the look-ahead guarantee for
free. So EOD history is translated into that shape instead of growing a parallel
pipeline that would drift.

WHAT THESE SNAPSHOTS ARE NOT
    One bar per contract per day. No bid, no ask, no intraday path. A signal
    reading OI velocity per minute, or crossing a spread, is meaningless here —
    hence `SessionPhase.END_OF_DAY`, so such a signal can refuse the bar rather
    than quietly computing nonsense from it.

Data traps handled (all measured on real files, see docs/BHAVCOPY.md):
  - an untraded contract republishes yesterday's close, so only traded rows
    with volume above a floor are emitted
  - expiring contracts are dropped: `expiry_dt > trad_dt`, matching the
    importer, because on expiry day the settlement column holds the index
    level rather than a premium
  - open interest is in SHARES here while the recorder's Fyers rows may be in
    contracts; the two are not mixed in one store for that reason

Materialised into a SEPARATE database on purpose. Writing EOD bars into
`market_data.db` would interleave one-per-day rows with the recorder's
one-per-minute rows on the same dates, and a replay would silently mix them.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Iterator, Optional

from config import NSE_EOD_DB
from market_hours import EXPIRY_TIME_IST, IST, SessionPhase
from recorder.models import ChainRow, ChainSnapshot

# Matches the importer's default: enough to exclude prints too thin to be a
# current price, low enough to keep far-month ATM strikes on quiet days.
DEFAULT_MIN_VOLUME = 25.0


def _eod_timestamp(trad_dt: str) -> str:
    """The session's close instant, so bars sort and join like recorder rows."""
    d = datetime.strptime(trad_dt, "%Y-%m-%d").date()
    return datetime.combine(d, EXPIRY_TIME_IST, tzinfo=IST).isoformat()


def _fyers_expiry(expiry_dt: str) -> str:
    """Archive dates are ISO; the rest of the app speaks Fyers DD-MM-YYYY."""
    return datetime.strptime(expiry_dt, "%Y-%m-%d").strftime("%d-%m-%Y")


def _expiry_epoch(expiry_dt: str) -> int:
    d = datetime.strptime(expiry_dt, "%Y-%m-%d").date()
    return int(datetime.combine(d, EXPIRY_TIME_IST, tzinfo=IST).timestamp())


def _oi_change_pct(oi: Optional[float], chg_oi: Optional[float]) -> float:
    """
    Reconstruct oichp (% change versus the prior day) from the absolute change.

    Fyers publishes this directly; the archive gives absolute `chg_oi`, so the
    prior level is `oi - chg_oi`. Returns 0.0 when the prior level is zero or
    unknown rather than a huge number off a near-zero base — that artefact is
    exactly what makes the live SHORT_BUILDUP rule fire on +2041% readings.
    """
    if oi is None or chg_oi is None:
        return 0.0
    prior = oi - chg_oi
    if prior <= 0:
        return 0.0
    return chg_oi / prior * 100.0


def iter_eod_snapshots(symbol: str, session_date: str,
                       src_db: str = NSE_EOD_DB,
                       min_volume: float = DEFAULT_MIN_VOLUME,
                       ) -> Iterator[ChainSnapshot]:
    """
    One snapshot per listed expiry for `symbol` on `session_date`.

    Emits nothing for a date with no traded rows, rather than an empty chain: a
    snapshot with no strikes is indistinguishable from a broken one downstream.
    """
    conn = sqlite3.connect(src_db)
    try:
        rows = conn.execute("""
            SELECT expiry_dt, strike, option_type, close, volume, oi, chg_oi,
                   underlying
            FROM daily_option
            WHERE symbol = ? AND trad_dt = ? AND expiry_dt > trad_dt
            ORDER BY expiry_dt, strike, option_type
        """, (symbol.upper(), session_date)).fetchall()
    except sqlite3.Error:
        return
    finally:
        conn.close()

    by_expiry: dict[str, list[ChainRow]] = defaultdict(list)
    spot_by_expiry: dict[str, float] = {}

    for expiry, strike, opt, close, volume, oi, chg_oi, underlying in rows:
        if not close or close <= 0:
            continue
        if not volume or volume < min_volume:
            continue
        if opt not in ("CE", "PE"):
            continue
        by_expiry[expiry].append(ChainRow(
            strike=float(strike), option_type=opt,
            oi=float(oi or 0.0),
            oi_change=float(chg_oi or 0.0),
            oi_change_pct=_oi_change_pct(oi, chg_oi),
            prev_oi=float((oi or 0.0) - (chg_oi or 0.0)),
            ltp=float(close),
            # No book in EOD data. Left at zero so `price_for_iv` falls back to
            # the close instead of inventing a mid, and so any spread-aware
            # cost model refuses the fill rather than assuming a tight market.
            bid=0.0, ask=0.0,
            volume=float(volume or 0.0),
        ))
        if underlying:
            spot_by_expiry[expiry] = float(underlying)

    ts = _eod_timestamp(session_date)
    for expiry in sorted(by_expiry):
        spot = spot_by_expiry.get(expiry)
        if not spot or spot <= 0:
            # Pre-2024-07-08 files publish no underlying price. Rebuilding it
            # from the futures rows is listed as not-done in docs/BHAVCOPY.md;
            # until then, a date without spot is skipped rather than guessed.
            continue
        yield ChainSnapshot(
            ts=ts, session_date=session_date,
            session_phase=SessionPhase.END_OF_DAY,
            symbol=symbol.upper(),
            expiry_date=_fyers_expiry(expiry),
            expiry_epoch=_expiry_epoch(expiry),
            spot=spot,
            rows=tuple(by_expiry[expiry]),
        )


def available_dates(symbol: str, src_db: str = NSE_EOD_DB) -> list[str]:
    """Trading dates the archive holds for a symbol, oldest first."""
    conn = sqlite3.connect(src_db)
    try:
        rows = conn.execute(
            "SELECT DISTINCT trad_dt FROM daily_option WHERE symbol = ? "
            "ORDER BY trad_dt", (symbol.upper(),)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [r[0] for r in rows]


def materialize(symbol: str, dest_db: str,
                dates: Optional[list[str]] = None,
                src_db: str = NSE_EOD_DB,
                min_volume: float = DEFAULT_MIN_VOLUME,
                nearest_expiry_only: bool = True) -> dict:
    """
    Write EOD snapshots into a recorder-schema database the backtester can read.

    `dest_db` must NOT be `market_data.db`: mixing one-per-day bars with the
    recorder's one-per-minute bars on the same dates would make a replay
    interleave them. Pass a separate file and hand it to `run_backtest` as
    `db_path`.

    `nearest_expiry_only` keeps one chain per date, which is what a
    directional daily signal wants. Term-structure work needs all of them.
    """
    from recorder.store import init_db, write_snapshot

    init_db(dest_db)
    dates = dates if dates is not None else available_dates(symbol, src_db)

    written_rows = 0
    written_snaps = 0
    skipped_dates = 0

    for d in sorted(dates):
        snaps = list(iter_eod_snapshots(symbol, d, src_db, min_volume))
        if not snaps:
            skipped_dates += 1
            continue
        if nearest_expiry_only:
            snaps = [min(snaps, key=lambda s: s.expiry_epoch)]
        for s in snaps:
            written_rows += write_snapshot(s, dest_db)
            written_snaps += 1

    return {
        "symbol": symbol.upper(),
        "dates_requested": len(dates),
        "dates_written": len(dates) - skipped_dates,
        "dates_skipped": skipped_dates,
        "snapshots": written_snaps,
        "rows": written_rows,
        "dest_db": dest_db,
    }


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Rebuild exchange EOD rows as ChainSnapshots for backtesting.")
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--source", default=NSE_EOD_DB)
    ap.add_argument("--dest", default="eod_snapshots.db",
                    help="separate from market_data.db on purpose")
    ap.add_argument("--min-volume", type=float, default=DEFAULT_MIN_VOLUME)
    ap.add_argument("--all-expiries", action="store_true",
                    help="keep every listed expiry, not just the nearest")
    args = ap.parse_args()

    stats = materialize(args.symbol, args.dest, src_db=args.source,
                        min_volume=args.min_volume,
                        nearest_expiry_only=not args.all_expiries)
    if not stats["snapshots"]:
        print(f"No snapshots written for {stats['symbol']}. Is {args.source} "
              f"populated? See docs/BHAVCOPY.md.")
        return 1
    for k, v in stats.items():
        print(f"{k:>16}: {v}")
    print(f"\nBacktest against it with db_path={args.dest!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
