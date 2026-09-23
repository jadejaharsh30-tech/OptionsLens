# optionslens/backend/bhavcopy/importer.py
"""
Load exchange EOD option history into the app's own stores.

Reads the SQLite file written by `bhavcopy.download` and writes, into DB_PATH:

  atm_iv_history     ATM IV for every listed expiry on every date, solved with
                     the same forward and ATM definition the live endpoint and
                     the daily job use (chain_pricing.atm_iv_for_chain).
  spot_history       the underlying's official close, as NSE publishes it
                     alongside each contract.
  contract_lot_size  the lot size of every contract, per expiry.

Two properties of the exchange data decide how prices are chosen, both measured
on 29 sessions of real files:

  - An untraded contract still gets a published close, and it is exactly the
    previous close, on every such row. Only traded rows are used, and a
    minimum-volume gate keeps out prints too thin to be current.
  - On expiry day the settlement column carries the underlying's settlement
    level, not an option price. Settlement is never read here; expiring
    contracts drop out anyway because no time value is left at the close.

Nothing is overwritten. Existing rows (including live ones) win, so an import
can be re-run or widened at any time.

Usage, from backend/:
    python3 -m bhavcopy.importer
    python3 -m bhavcopy.importer --source ~/Downloads/nse_options_eod.db
    python3 -m bhavcopy.importer --symbols ALL --min-volume 25
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from chain_pricing import atm_iv_for_chain
from config import DB_PATH, NSE_EOD_DB, UNDERLYINGS
from eod_vol import constant_maturity_iv
from lot_sizes import record_lot_sizes
from market_hours import EXPIRY_TIME_IST, IST, time_to_expiry
from snapshot_store import init_db, write_atm_iv_many, write_spot_many

SOURCE = "bhavcopy"

# Contracts traded below this are excluded. Measured on 29 real sessions:
#   - Moving the gate between 1 and 50 contracts shifted NIFTY's 30-day ATM IV
#     by a median of 0.02 vol points. ATM strikes on a liquid index trade far
#     above any of these thresholds, so the gate only decides the thin cases.
#   - In those thin cases a high gate costs coverage outright: MIDCPNIFTY's
#     second month traded 16-900 contracts a day at the money, and a gate of 50
#     halved the dates with a 30-day reading.
#   - A stale print is less harmful here than it looks, because the forward is
#     implied from the same pair of closes; a pair that traded early solves to
#     the IV of that moment, not to nonsense.
# 10 sits where settlement stops disagreeing with the close on most traded rows.
DEFAULT_MIN_VOLUME = 10


@dataclass
class SymbolStats:
    dates: int = 0
    dates_without_spot: int = 0
    expiries_seen: int = 0
    expiries_solved: int = 0
    dates_with_cm30: int = 0
    iv_rows_added: int = 0
    spot_rows_added: int = 0
    first: Optional[str] = None
    last: Optional[str] = None
    cm30: list[float] = field(default_factory=list)


def import_history(source_db: str, target_db: str = DB_PATH,
                   symbols: Optional[set[str]] = None,
                   min_volume: float = DEFAULT_MIN_VOLUME,
                   progress: bool = False) -> dict[str, SymbolStats]:
    """
    Import every date for `symbols` (default: configured underlyings present in
    the source) and return per-symbol statistics.
    """
    src = _open_read_only(source_db)
    init_db(target_db)

    available = {r[0] for r in src.execute("SELECT DISTINCT symbol FROM daily_option")}
    wanted = (available & set(UNDERLYINGS)) if symbols is None else (available & symbols)

    stats: dict[str, SymbolStats] = {}
    for symbol in sorted(wanted):
        stats[symbol] = _import_symbol(src, target_db, symbol, min_volume, progress)

    lot_rows = src.execute("""
        SELECT symbol, expiry_dt, CAST(lot_size AS INTEGER), MIN(trad_dt), MAX(trad_dt),
               COUNT(*) AS n
        FROM daily_option
        WHERE lot_size IS NOT NULL AND lot_size > 0
        GROUP BY symbol, expiry_dt, CAST(lot_size AS INTEGER)
        ORDER BY symbol, expiry_dt, n DESC
    """).fetchall()
    src.close()

    # One lot size per contract: the one carried by most of its rows. They
    # should never disagree within an expiry; if they do, the majority wins.
    chosen: dict[tuple[str, str], tuple] = {}
    for sym, exp, lot, first, last, _n in lot_rows:
        if sym in wanted and (sym, exp) not in chosen:
            chosen[(sym, exp)] = (sym, exp, lot, first, last)
    record_lot_sizes(chosen.values(), db_path=target_db, source=SOURCE)
    return stats


def _import_symbol(src: sqlite3.Connection, target_db: str, symbol: str,
                   min_volume: float, progress: bool) -> SymbolStats:
    st = SymbolStats()
    dates = [r[0] for r in src.execute(
        "SELECT DISTINCT trad_dt FROM daily_option WHERE symbol = ? ORDER BY trad_dt",
        (symbol,))]

    iv_rows: list[tuple[str, str, str, float]] = []
    spot_rows: list[tuple[str, str, float]] = []

    for i, day in enumerate(dates):
        st.dates += 1
        st.first = st.first or day
        st.last = day

        rows = src.execute("""
            SELECT expiry_dt, strike, option_type, close, volume, underlying
            FROM daily_option
            WHERE symbol = ? AND trad_dt = ? AND expiry_dt > trad_dt
        """, (symbol, day)).fetchall()

        spot = next((r[5] for r in rows if r[5]), None)
        if not spot:
            # Pre-2024-07-08 files publish no underlying price. Rebuilding spot
            # from futures is its own piece of work; skip rather than guess.
            st.dates_without_spot += 1
            continue
        spot_rows.append((day, symbol, spot))

        chains: dict[str, list[dict]] = defaultdict(list)
        for expiry, strike, opt, close, volume, _u in rows:
            if not close or close <= 0 or not volume or volume < min_volume:
                continue
            chains[expiry].append({"strike": strike, "option_type": opt,
                                   "ltp": close, "bid": 0, "ask": 0})

        at_close = datetime.combine(date.fromisoformat(day), EXPIRY_TIME_IST, tzinfo=IST)
        points = []
        st.expiries_seen += len({r[0] for r in rows})
        for expiry, chain in chains.items():
            fyers_expiry = date.fromisoformat(expiry).strftime("%d-%m-%Y")
            T = time_to_expiry(fyers_expiry, now=at_close)
            if T <= 0:
                continue
            iv = atm_iv_for_chain(chain, T, spot)
            if iv is None:
                continue
            st.expiries_solved += 1
            iv_rows.append((day, symbol, fyers_expiry, iv))
            points.append((T * 365.0, iv))

        cm = constant_maturity_iv(points)
        if cm is not None:
            st.dates_with_cm30 += 1
            st.cm30.append(cm)

        if progress and (i + 1) % 50 == 0:
            print(f"  {symbol:<11} {i + 1:>5}/{len(dates)} dates", flush=True)

    st.iv_rows_added = write_atm_iv_many(target_db, iv_rows, SOURCE)
    st.spot_rows_added = write_spot_many(target_db, spot_rows)
    return st


def _open_read_only(path: str) -> sqlite3.Connection:
    """Read-only, so an import can never modify the archive. as_uri() rather
    than an f-string: Windows paths need file:///C:/... form."""
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def _print_report(stats: dict[str, SymbolStats], min_volume: float) -> None:
    print(f"\nminimum volume per contract: {min_volume:g}\n")
    print(f"{'symbol':<11}{'dates':>7}{'no spot':>9}{'expiries':>10}{'solved':>8}"
          f"{'CM30 days':>11}{'CM30 lo':>9}{'CM30 hi':>9}{'IV rows':>9}{'spot':>7}")
    for sym, st in stats.items():
        solved = st.expiries_solved / st.expiries_seen * 100 if st.expiries_seen else 0
        lo = f"{min(st.cm30) * 100:.1f}%" if st.cm30 else "—"
        hi = f"{max(st.cm30) * 100:.1f}%" if st.cm30 else "—"
        print(f"{sym:<11}{st.dates:>7,}{st.dates_without_spot:>9,}"
              f"{st.expiries_seen:>10,}{solved:>7.0f}%{st.dates_with_cm30:>11,}"
              f"{lo:>9}{hi:>9}{st.iv_rows_added:>9,}{st.spot_rows_added:>7,}")
    print("\n'solved' is the share of listed expiries that produced an ATM IV after the")
    print("volume gate; far months often have no traded ATM strike, which is expected.")
    print("'CM30 days' is how many dates now have a 30-day reading for IV Rank.")
    print("IV rows and spot count only rows added by this run; re-runs add nothing.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=NSE_EOD_DB,
                    help="bhavcopy database (default: $NSE_EOD_DB or nse_options_eod.db)")
    ap.add_argument("--target", default=DB_PATH,
                    help="app database (default: $DB_PATH or optionslens.db)")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated, or ALL; default is every configured underlying")
    ap.add_argument("--min-volume", type=float, default=DEFAULT_MIN_VOLUME)
    args = ap.parse_args()

    if not Path(args.source).is_file():
        ap.error(f"source database not found: {args.source}")

    symbols = None
    if args.symbols:
        symbols = (None if args.symbols.strip().upper() == "ALL"
                   else {s.strip().upper() for s in args.symbols.split(",") if s.strip()})
        if symbols is None:
            src = _open_read_only(args.source)
            symbols = {r[0] for r in src.execute("SELECT DISTINCT symbol FROM daily_option")}
            src.close()

    print(f"source  {Path(args.source).resolve()}")
    print(f"target  {Path(args.target).resolve()}")
    stats = import_history(args.source, args.target, symbols, args.min_volume, progress=True)
    if not stats:
        print("\nNo matching symbols in the source database.")
        return 1
    _print_report(stats, args.min_volume)
    return 0


if __name__ == "__main__":
    sys.exit(main())
