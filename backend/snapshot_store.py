# optionslens/backend/snapshot_store.py
"""
SQLite store for daily IV snapshots and spot price history.
Used to calculate IV Rank (IVR) and Realized Volatility.

`atm_iv_history` holds one row per (date, symbol, EXPIRY). Anything that turns
it into a single series per date must go through `get_cm_iv_history`, which
interpolates each date's expiries to a fixed tenor. Reading `iv` straight off
the table mixes 5-day and 100-day vol into one distribution the moment a date
carries more than one expiry, which the bhavcopy importer and the daily job
both now write.
"""
import sqlite3
from datetime import datetime
from typing import Iterable, Optional

from config import DB_PATH
from eod_vol import DEFAULT_TARGET_DAYS, constant_maturity_iv, percentile_rank
from market_hours import IST, EXPIRY_TIME_IST, time_to_expiry


def init_db(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS iv_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            symbol        TEXT NOT NULL,
            expiry_date   TEXT NOT NULL,
            strike        REAL NOT NULL,
            option_type   TEXT NOT NULL,
            iv            REAL,
            ltp           REAL,
            oi            REAL,
            UNIQUE(snapshot_date, symbol, expiry_date, strike, option_type)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS atm_iv_history (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            symbol        TEXT NOT NULL,
            expiry_date   TEXT NOT NULL,
            iv            REAL,
            UNIQUE(snapshot_date, symbol, expiry_date)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS spot_history (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            symbol        TEXT NOT NULL,
            spot          REAL NOT NULL,
            UNIQUE(snapshot_date, symbol)
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_spot_history_symbol_date
        ON spot_history(symbol, snapshot_date DESC)
    """)
    # Provenance. Live rows are solved from bid-ask mids at 15:10; bhavcopy rows
    # from the exchange closing price. Close enough to share a series, different
    # enough that an audit must be able to tell them apart.
    cols = {r[1] for r in c.execute("PRAGMA table_info(atm_iv_history)")}
    if "source" not in cols:
        c.execute("ALTER TABLE atm_iv_history ADD COLUMN source TEXT")
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_atm_iv_symbol_date
        ON atm_iv_history(symbol, snapshot_date)
    """)
    conn.commit()
    conn.close()


def write_iv_snapshot(db_path: str, snapshot_date: str, symbol: str,
                      expiry_date: str, strike: float, option_type: str,
                      iv: float, ltp: float, oi: float):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT OR IGNORE INTO iv_snapshots
            (snapshot_date, symbol, expiry_date, strike, option_type, iv, ltp, oi)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (snapshot_date, symbol, expiry_date, strike, option_type, iv, ltp, oi))
    conn.commit()
    conn.close()


def write_atm_iv(db_path: str, snapshot_date: str, symbol: str,
                 expiry_date: str, atm_iv: float, source: str = "live"):
    write_atm_iv_many(db_path, [(snapshot_date, symbol, expiry_date, atm_iv)], source)


def write_atm_iv_many(db_path: str,
                      rows: Iterable[tuple[str, str, str, float]],
                      source: str) -> int:
    """
    Bulk insert (snapshot_date ISO, symbol, expiry_date DD-MM-YYYY, iv decimal).

    INSERT OR IGNORE: an existing row for the same date and expiry is never
    overwritten, so re-running an import is a no-op and live rows win over
    backfilled ones. Returns the number of rows actually added.
    """
    conn = sqlite3.connect(db_path)
    before = conn.total_changes
    conn.executemany("""
        INSERT OR IGNORE INTO atm_iv_history
            (snapshot_date, symbol, expiry_date, iv, source)
        VALUES (?, ?, ?, ?, ?)
    """, [(d, sym, exp, iv, source) for d, sym, exp, iv in rows])
    conn.commit()
    added = conn.total_changes - before
    conn.close()
    return added


def write_spot_price(db_path: str, snapshot_date: str,
                     symbol: str, spot: float):
    """
    Persist a daily closing price.

    Only ever call this with an OFFICIAL close (post-CAS), never with an
    intraday LTP. Under CAS an F&O-eligible stock has no continuous trading
    from 15:15, so a price sampled during the auction window is a stale
    pre-auction print masquerading as a close.
    """
    write_spot_many(db_path, [(snapshot_date, symbol, spot)])


def write_spot_many(db_path: str,
                    rows: Iterable[tuple[str, str, float]]) -> int:
    """Bulk (snapshot_date, symbol, official close). Never overwrites a row."""
    conn = sqlite3.connect(db_path)
    before = conn.total_changes
    conn.executemany("""
        INSERT OR IGNORE INTO spot_history (snapshot_date, symbol, spot)
        VALUES (?, ?, ?)
    """, list(rows))
    conn.commit()
    added = conn.total_changes - before
    conn.close()
    return added


def get_atm_iv_history(db_path: str, symbol: str,
                       days: int = 252) -> list[dict]:
    """
    Per-expiry ATM IV rows for the most recent `days` DATES, most recent first.

    `days` counts distinct trading dates, not rows. It used to be a row LIMIT,
    which silently shrank the window as soon as a date carried several expiries.
    Each row carries its expiry; callers wanting one number per date want
    `get_cm_iv_history` instead.
    """
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT snapshot_date, expiry_date, iv FROM atm_iv_history
        WHERE symbol = ? AND snapshot_date IN (
            SELECT DISTINCT snapshot_date FROM atm_iv_history
            WHERE symbol = ? ORDER BY snapshot_date DESC LIMIT ?)
        ORDER BY snapshot_date DESC, expiry_date
    """, (symbol, symbol, days)).fetchall()
    conn.close()
    return [{"date": r[0], "expiry_date": r[1], "iv": r[2]} for r in rows]


def get_cm_iv_history(db_path: str, symbol: str, days: int = 252,
                      target_days: float = DEFAULT_TARGET_DAYS) -> list[dict]:
    """
    One constant-maturity ATM IV per date, most recent first.

    Each date's expiries are read as (tenor, iv) points, tenor measured from
    that date's 15:30 IST close to the expiry instant, and interpolated to
    `target_days`. Dates where the target cannot be reached are omitted rather
    than filled; `days` still counts them, so the window is a calendar of
    trading dates, not a quota of successful readings.
    """
    by_date: dict[str, list[tuple[float, float]]] = {}
    for row in get_atm_iv_history(db_path, symbol, days):
        tenor = _tenor_days(row["date"], row["expiry_date"])
        if tenor is None or tenor <= 0 or row["iv"] is None:
            continue
        by_date.setdefault(row["date"], []).append((tenor, row["iv"]))

    out = []
    for d in sorted(by_date, reverse=True):
        iv = constant_maturity_iv(by_date[d], target_days)
        if iv is not None:
            out.append({"date": d, "iv": iv})
    return out


def get_spot_history(db_path: str, symbol: str,
                     days: int = 252) -> list[dict]:
    """Return spot price history for a symbol, most recent first."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT snapshot_date, spot FROM spot_history
        WHERE symbol = ?
        ORDER BY snapshot_date DESC
        LIMIT ?
    """, (symbol, days)).fetchall()
    conn.close()
    return [{"date": r[0], "spot": r[1]} for r in rows]


def get_iv_percentile(db_path: str, symbol: str, current_iv: float,
                      days: int = 252,
                      target_days: float = DEFAULT_TARGET_DAYS) -> Optional[float]:
    """
    Percentile of `current_iv` within the last `days` dates of constant-maturity
    ATM IV. `current_iv` must be measured at the same tenor.

    Replaces the old IV Rank, (IV − low) / (high − low), which had two faults:
    it ranked front-expiry IV, so it largely measured where the weekly expiry
    cycle stood, and one spike day set the range for a year and pinned every
    later reading near zero. Percentile rank counts observations instead.

    None until 20 dated readings exist.
    """
    history = get_cm_iv_history(db_path, symbol, days, target_days)
    return percentile_rank([h["iv"] for h in history], current_iv)


def _tenor_days(snapshot_date: str, expiry_date: str) -> Optional[float]:
    """Calendar days from the snapshot date's close to the expiry instant."""
    try:
        d = datetime.strptime(snapshot_date, "%Y-%m-%d").date()
        close = datetime.combine(d, EXPIRY_TIME_IST, tzinfo=IST)
        return time_to_expiry(expiry_date, now=close) * 365.0
    except (ValueError, TypeError):
        return None
