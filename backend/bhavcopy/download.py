#!/usr/bin/env python3
# optionslens/backend/bhavcopy/download.py
"""
NSE options EOD backfill — standalone, stdlib only, append-only SQLite.

Downloads the NSE derivatives bhavcopy for a date range, keeps the option rows
(which the exchange publishes and most downloaders discard), and stores them raw.
Index futures for the same symbols are stored alongside, because the same file
carries them and forward-based pricing wants them.

Schema eras are handled transparently: NSE switched to the UDiFF format on
2024-07-08. Column names, date formats and turnover units all differ across that
boundary. Both are verified against live downloads.

Nothing is derived here. Implied vol, Greeks and GEX are computed at read time by
the existing engines, so fixing the pricing model retroactively improves the whole
history rather than invalidating it.

This module deliberately imports nothing from the rest of the backend, so it
can be copied out and run on any machine with Python 3.8+. NSE blocks most
datacenter and VPN ranges, so in practice it runs on a home connection.

Usage (from backend/, or with this file copied anywhere):
    python3 -m bhavcopy.download --from 2026-06-01 --to 2026-09-10
    python3 -m bhavcopy.download --from 2024-07-08 --to 2026-09-10 --stocks --symbols NIFTY,BANKNIFTY,RELIANCE
    python3 -m bhavcopy.download --report

The database path defaults to $NSE_EOD_DB, falling back to nse_options_eod.db
in the working directory. Load the result into the app with bhavcopy.importer.

Pre-2024-07-08 (legacy format) caveats, not yet handled:
  - Some years (2003, 2005 and 2006 were observed) spell the option-type column
    differently. extract() reads OPTION_TYP only, so those years would silently
    keep zero option rows.
  - Legacy files publish no underlying price; spot must come from futures.

Re-running a range is safe and cheap: completed dates are skipped, and rows are
written with INSERT OR IGNORE, so a partial run resumes without duplicating.

That skip is by date, not by scope, so WIDENING the symbol set later (adding
stocks, say) would silently skip every date already ingested and add nothing.
Pass --force to re-download dates that are already logged. Decide the scope up
front where you can; --force costs a full re-download of the range.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sqlite3
import sys
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

UDIFF_START = date(2024, 7, 8)

DEFAULT_DB = os.getenv("NSE_EOD_DB", "nse_options_eod.db")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

# Instrument codes per era: index options, stock options, index futures,
# stock futures. Confirmed present in live files from both eras.
CODES = {
    "udiff":  {"idx_opt": "IDO", "stk_opt": "STO", "idx_fut": "IDF", "stk_fut": "STF"},
    "legacy": {"idx_opt": "OPTIDX", "stk_opt": "OPTSTK",
               "idx_fut": "FUTIDX", "stk_fut": "FUTSTK"},
}

DEFAULT_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_option (
    trad_dt        TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    expiry_dt      TEXT NOT NULL,
    actl_expiry_dt TEXT,
    strike         REAL NOT NULL,
    option_type    TEXT NOT NULL,
    open           REAL,
    high           REAL,
    low            REAL,
    close          REAL,
    last           REAL,
    prev_close     REAL,
    settle         REAL,
    underlying     REAL,
    oi             REAL,
    chg_oi         REAL,
    volume         REAL,
    num_trades     REAL,
    turnover_raw   REAL,
    lot_size       REAL,
    instrument     TEXT,
    era            TEXT NOT NULL,
    PRIMARY KEY (trad_dt, symbol, expiry_dt, strike, option_type)
);
CREATE INDEX IF NOT EXISTS ix_option_sym_date
    ON daily_option (symbol, trad_dt);
CREATE INDEX IF NOT EXISTS ix_option_sym_expiry
    ON daily_option (symbol, expiry_dt);

CREATE TABLE IF NOT EXISTS daily_future (
    trad_dt        TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    expiry_dt      TEXT NOT NULL,
    actl_expiry_dt TEXT,
    open           REAL,
    high           REAL,
    low            REAL,
    close          REAL,
    last           REAL,
    prev_close     REAL,
    settle         REAL,
    underlying     REAL,
    oi             REAL,
    chg_oi         REAL,
    volume         REAL,
    lot_size       REAL,
    instrument     TEXT,
    era            TEXT NOT NULL,
    PRIMARY KEY (trad_dt, symbol, expiry_dt)
);

-- One row per date attempted. 'no_file' is a holiday or non-trading day and is
-- a successful outcome, not an error: it stops the date being retried forever.
CREATE TABLE IF NOT EXISTS ingest_log (
    trad_dt      TEXT PRIMARY KEY,
    era          TEXT,
    url          TEXT,
    status       TEXT NOT NULL,
    rows_in_file INTEGER,
    options_kept INTEGER,
    futures_kept INTEGER,
    note         TEXT,
    ingested_at  TEXT NOT NULL
);
"""


# ── source resolution ────────────────────────────────────────────────────────

def era_for(day: date) -> str:
    return "legacy" if day < UDIFF_START else "udiff"


def url_for(day: date) -> str:
    if era_for(day) == "legacy":
        mmm = day.strftime("%b").upper()
        name = f"fo{day:%d}{mmm}{day:%Y}bhav.csv.zip"
        return ("https://nsearchives.nseindia.com/content/historical/"
                f"DERIVATIVES/{day:%Y}/{mmm}/{name}")
    return ("https://nsearchives.nseindia.com/content/fo/"
            f"BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv.zip")


def fetch(url: str, retries: int = 3, timeout: int = 90) -> bytes | None:
    """Return archive bytes, or None when NSE has no file for this date.

    A 404 means a non-trading day. Anything else is retried with backoff, since
    NSE throttles bursts and a transient failure must not look like a holiday.
    """
    delay = 2.0
    for attempt in range(1, retries + 1):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=timeout) as resp:
                return resp.read()
        except HTTPError as exc:
            if exc.code == 404:
                return None
            if attempt == retries:
                raise
        except URLError:
            if attempt == retries:
                raise
        time.sleep(delay)
        delay *= 2
    return None


def rows_from_zip(payload: bytes) -> list[dict[str, str]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        with zf.open(zf.namelist()[0]) as fp:
            text = fp.read().decode("utf-8", errors="replace")
    return [
        {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        for row in csv.DictReader(io.StringIO(text))
    ]


# ── parsing helpers ──────────────────────────────────────────────────────────

def num(value: str) -> float | None:
    """Parse a numeric cell. Blank, '-' and unparseable become None, never 0.0.

    The distinction matters: a genuine zero close and an unpublished field mean
    different things downstream, and collapsing them hides missing data.
    """
    if value is None:
        return None
    text = value.strip()
    if not text or text in {"-", "NA", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def iso_date(value: str, era: str) -> str | None:
    """Normalise a published date to YYYY-MM-DD.

    UDiFF publishes ISO already. Legacy publishes DD-Mon-YYYY with inconsistent
    month capitalisation ('25-Jul-2024' in EXPIRY_DT, '27-JUN-2024' in TIMESTAMP),
    so the legacy branch title-cases before parsing.
    """
    text = (value or "").strip()
    if not text:
        return None
    if era == "udiff":
        for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.title(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def extract(row: dict[str, str], era: str) -> dict:
    """Map one published row onto our column names, without deriving anything."""
    if era == "udiff":
        return {
            "trad_dt":        iso_date(row.get("TradDt", ""), era),
            "symbol":         row.get("TckrSymb", "").upper(),
            "expiry_dt":      iso_date(row.get("XpryDt", ""), era),
            "actl_expiry_dt": iso_date(row.get("FininstrmActlXpryDt", ""), era),
            "strike":         num(row.get("StrkPric", "")),
            "option_type":    row.get("OptnTp", "").upper(),
            "open":           num(row.get("OpnPric", "")),
            "high":           num(row.get("HghPric", "")),
            "low":            num(row.get("LwPric", "")),
            "close":          num(row.get("ClsPric", "")),
            "last":           num(row.get("LastPric", "")),
            "prev_close":     num(row.get("PrvsClsgPric", "")),
            "settle":         num(row.get("SttlmPric", "")),
            "underlying":     num(row.get("UndrlygPric", "")),
            "oi":             num(row.get("OpnIntrst", "")),
            "chg_oi":         num(row.get("ChngInOpnIntrst", "")),
            "volume":         num(row.get("TtlTradgVol", "")),
            "num_trades":     num(row.get("TtlNbOfTxsExctd", "")),
            "turnover_raw":   num(row.get("TtlTrfVal", "")),
            "lot_size":       num(row.get("NewBrdLotQty", "")),
            "instrument":     row.get("FinInstrmTp", "").upper(),
            "era":            era,
        }
    return {
        "trad_dt":        iso_date(row.get("TIMESTAMP", ""), era),
        "symbol":         row.get("SYMBOL", "").upper(),
        "expiry_dt":      iso_date(row.get("EXPIRY_DT", ""), era),
        "actl_expiry_dt": None,
        "strike":         num(row.get("STRIKE_PR", "")),
        "option_type":    row.get("OPTION_TYP", "").upper(),
        "open":           num(row.get("OPEN", "")),
        "high":           num(row.get("HIGH", "")),
        "low":            num(row.get("LOW", "")),
        "close":          num(row.get("CLOSE", "")),
        "last":           None,          # not published in this era
        "prev_close":     None,          # not published in this era
        "settle":         num(row.get("SETTLE_PR", "")),
        "underlying":     None,          # not published in this era
        "oi":             num(row.get("OPEN_INT", "")),
        "chg_oi":         num(row.get("CHG_IN_OI", "")),
        "volume":         num(row.get("CONTRACTS", "")),
        "num_trades":     None,
        "turnover_raw":   num(row.get("VAL_INLAKH", "")),   # lakhs, not rupees
        "lot_size":       None,
        "instrument":     row.get("INSTRUMENT", "").upper(),
        "era":            era,
    }


# ── storage ──────────────────────────────────────────────────────────────────

OPTION_COLS = [
    "trad_dt", "symbol", "expiry_dt", "actl_expiry_dt", "strike", "option_type",
    "open", "high", "low", "close", "last", "prev_close", "settle", "underlying",
    "oi", "chg_oi", "volume", "num_trades", "turnover_raw", "lot_size",
    "instrument", "era",
]
FUTURE_COLS = [
    "trad_dt", "symbol", "expiry_dt", "actl_expiry_dt",
    "open", "high", "low", "close", "last", "prev_close", "settle", "underlying",
    "oi", "chg_oi", "volume", "lot_size", "instrument", "era",
]


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def insert(conn: sqlite3.Connection, table: str, cols: list[str],
           records: list[dict]) -> int:
    if not records:
        return 0
    sql = (f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) "
           f"VALUES ({','.join('?' * len(cols))})")
    before = conn.total_changes
    conn.executemany(sql, [[r.get(c) for c in cols] for r in records])
    return conn.total_changes - before


def already_done(conn: sqlite3.Connection, day: date) -> bool:
    row = conn.execute(
        "SELECT status FROM ingest_log WHERE trad_dt = ?", (day.isoformat(),)
    ).fetchone()
    return row is not None and row[0] in {"ok", "no_file"}


def log(conn: sqlite3.Connection, day: date, era: str, url: str, status: str,
        rows_in_file: int = 0, opts: int = 0, futs: int = 0,
        note: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ingest_log "
        "(trad_dt, era, url, status, rows_in_file, options_kept, futures_kept, "
        " note, ingested_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (day.isoformat(), era, url, status, rows_in_file, opts, futs, note,
         datetime.now().isoformat(timespec="seconds")),
    )


# ── ingest ───────────────────────────────────────────────────────────────────

def ingest_day(conn: sqlite3.Connection, day: date, symbols: set[str] | None,
               include_stocks: bool) -> tuple[str, int, int]:
    era = era_for(day)
    url = url_for(day)
    codes = CODES[era]

    payload = fetch(url)
    if payload is None:
        log(conn, day, era, url, "no_file", note="404 — holiday or non-trading day")
        return "no_file", 0, 0

    try:
        raw = rows_from_zip(payload)
    except (zipfile.BadZipFile, IndexError) as exc:
        log(conn, day, era, url, "error", note=f"unreadable archive: {exc}")
        return "error", 0, 0

    opt_codes = {codes["idx_opt"]} | ({codes["stk_opt"]} if include_stocks else set())
    fut_codes = {codes["idx_fut"]} | ({codes["stk_fut"]} if include_stocks else set())

    options: list[dict] = []
    futures: list[dict] = []
    for row in raw:
        rec = extract(row, era)
        if symbols is not None and rec["symbol"] not in symbols:
            continue
        if not rec["trad_dt"] or not rec["expiry_dt"]:
            continue
        if rec["instrument"] in opt_codes:
            if rec["strike"] is None or rec["option_type"] not in {"CE", "PE"}:
                continue
            options.append(rec)
        elif rec["instrument"] in fut_codes:
            futures.append(rec)

    n_opt = insert(conn, "daily_option", OPTION_COLS, options)
    n_fut = insert(conn, "daily_future", FUTURE_COLS, futures)
    log(conn, day, era, url, "ok", len(raw), n_opt, n_fut)
    conn.commit()
    return "ok", n_opt, n_fut


def run(conn: sqlite3.Connection, start: date, end: date, symbols: set[str] | None,
        include_stocks: bool, pause: float, force: bool = False) -> None:
    day = start
    stats = {"ok": 0, "no_file": 0, "skipped": 0, "error": 0}
    total_opt = total_fut = 0

    while day <= end:
        if day.weekday() >= 5:                      # exchange is shut at weekends
            day += timedelta(days=1)
            continue
        if not force and already_done(conn, day):
            stats["skipped"] += 1
            day += timedelta(days=1)
            continue

        try:
            status, n_opt, n_fut = ingest_day(conn, day, symbols, include_stocks)
        except Exception as exc:
            status, n_opt, n_fut = "error", 0, 0
            log(conn, day, era_for(day), url_for(day), "error",
                note=f"{type(exc).__name__}: {exc}")
            conn.commit()

        stats[status] = stats.get(status, 0) + 1
        total_opt += n_opt
        total_fut += n_fut

        if status == "ok":
            print(f"{day}  {era_for(day):<6}  options {n_opt:>7,}  futures {n_fut:>5,}")
        elif status == "no_file":
            print(f"{day}  {era_for(day):<6}  no file (holiday)")
        else:
            print(f"{day}  {era_for(day):<6}  ERROR — see ingest_log")

        day += timedelta(days=1)
        time.sleep(pause)                           # stay polite to the archive

    print()
    print(f"days ingested   {stats['ok']:,}")
    print(f"days skipped    {stats['skipped']:,}  (already in the database)")
    print(f"non-trading     {stats['no_file']:,}")
    print(f"errors          {stats['error']:,}")
    print(f"option rows     {total_opt:,}")
    print(f"future rows     {total_fut:,}")


# ── report ───────────────────────────────────────────────────────────────────

def report(conn: sqlite3.Connection) -> None:
    def one(sql: str, *args):
        row = conn.execute(sql, args).fetchone()
        return row if row else ()

    n_opt, n_fut = (
        one("SELECT COUNT(*) FROM daily_option")[0],
        one("SELECT COUNT(*) FROM daily_future")[0],
    )
    print(f"option rows   {n_opt:,}")
    print(f"future rows   {n_fut:,}")
    if not n_opt:
        print("\nNothing ingested yet.")
        return

    lo, hi, days = one(
        "SELECT MIN(trad_dt), MAX(trad_dt), COUNT(DISTINCT trad_dt) FROM daily_option"
    )
    print(f"date range    {lo} to {hi}  ({days:,} trading days)")

    print("\nper symbol:")
    print(f"   {'symbol':<12}{'rows':>12}{'days':>8}{'expiries':>10}"
          f"{'first':>13}{'last':>13}")
    for sym, rows, dd, ex, f, l in conn.execute(
        "SELECT symbol, COUNT(*), COUNT(DISTINCT trad_dt), "
        "       COUNT(DISTINCT expiry_dt), MIN(trad_dt), MAX(trad_dt) "
        "FROM daily_option GROUP BY symbol ORDER BY COUNT(*) DESC LIMIT 15"
    ):
        print(f"   {sym:<12}{rows:>12,}{dd:>8,}{ex:>10,}{f:>13}{l:>13}")

    # Liquidity gate: rows with no traded volume carry a model-derived settlement
    # price, so their implied vol would be NSE's model rather than the market's.
    traded, = one("SELECT COUNT(*) FROM daily_option WHERE volume > 0")
    print(f"\ntraded rows   {traded:,} of {n_opt:,} "
          f"({traded / n_opt * 100:.1f}%) have volume > 0")

    diverged, = one(
        "SELECT COUNT(*) FROM daily_option "
        "WHERE volume > 0 AND close > 0 AND settle > 0 "
        "  AND ABS(settle - close) / close > 0.01"
    )
    if traded:
        print(f"settle≠close  {diverged:,} of {traded:,} traded rows "
              f"({diverged / traded * 100:.1f}%) differ by more than 1%")

    gaps = conn.execute(
        "SELECT status, COUNT(*) FROM ingest_log GROUP BY status"
    ).fetchall()
    print("\ningest log:")
    for status, n in gaps:
        print(f"   {status:<12}{n:>8,}")

    errs = conn.execute(
        "SELECT trad_dt, note FROM ingest_log WHERE status='error' "
        "ORDER BY trad_dt LIMIT 10"
    ).fetchall()
    if errs:
        print("\nfailed dates (re-run the range to retry):")
        for d, note in errs:
            print(f"   {d}  {note}")


# ── cli ──────────────────────────────────────────────────────────────────────

def parse_day(text: str) -> date:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not YYYY-MM-DD")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", type=parse_day)
    ap.add_argument("--to", dest="end", type=parse_day)
    ap.add_argument("--db", type=Path, default=Path(DEFAULT_DB),
                    help="SQLite file (default: $NSE_EOD_DB or nse_options_eod.db)")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                    help="comma-separated, or ALL for every underlying")
    ap.add_argument("--stocks", action="store_true",
                    help="also keep single-stock options and futures")
    ap.add_argument("--pause", type=float, default=0.8,
                    help="seconds between dates (default 0.8)")
    ap.add_argument("--force", action="store_true",
                    help="re-download dates already logged; needed when widening "
                         "the symbol set, since the resume check is by date")
    ap.add_argument("--report", action="store_true",
                    help="summarise the database and exit")
    args = ap.parse_args()

    conn = connect(args.db)
    try:
        if args.report:
            report(conn)
            return 0

        if not args.start or not args.end:
            ap.error("--from and --to are required unless --report is given")
        if args.end < args.start:
            ap.error("--to is before --from")

        symbols = (None if args.symbols.strip().upper() == "ALL"
                   else {s.strip().upper() for s in args.symbols.split(",") if s.strip()})

        print(f"database   {args.db.resolve()}")
        print(f"range      {args.start} to {args.end}")
        print(f"symbols    {'ALL' if symbols is None else ', '.join(sorted(symbols))}")
        print(f"stocks     {'included' if args.stocks else 'index only'}")
        if args.force:
            print("force      on — dates already ingested will be re-downloaded")
        print()
        run(conn, args.start, args.end, symbols, args.stocks, args.pause, args.force)
        print("\nRun with --report for a summary of what landed.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
