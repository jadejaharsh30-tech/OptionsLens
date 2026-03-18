# optionslens/backend/snapshot_store.py
"""
SQLite store for daily IV snapshots and spot price history.
Used to calculate IV Rank (IVR) and Realized Volatility.
"""
import sqlite3
from typing import Optional
from config import DB_PATH


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
                 expiry_date: str, atm_iv: float):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT OR IGNORE INTO atm_iv_history
            (snapshot_date, symbol, expiry_date, iv)
        VALUES (?, ?, ?, ?)
    """, (snapshot_date, symbol, expiry_date, atm_iv))
    conn.commit()
    conn.close()


def write_spot_price(db_path: str, snapshot_date: str,
                     symbol: str, spot: float):
    """Persist today's closing spot price for realized vol computation."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT OR IGNORE INTO spot_history (snapshot_date, symbol, spot)
        VALUES (?, ?, ?)
    """, (snapshot_date, symbol, spot))
    conn.commit()
    conn.close()


def get_atm_iv_history(db_path: str, symbol: str,
                       days: int = 252) -> list[dict]:
    """Return ATM IV history for a symbol, most recent `days` entries."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT snapshot_date, iv FROM atm_iv_history
        WHERE symbol = ?
        ORDER BY snapshot_date DESC
        LIMIT ?
    """, (symbol, days)).fetchall()
    conn.close()
    return [{"date": r[0], "iv": r[1]} for r in rows]


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


def get_iv_rank(db_path: str, symbol: str,
                current_iv: float, days: int = 252) -> Optional[float]:
    """
    IV Rank = (current_IV - period_low) / (period_high - period_low) × 100

    Returns None if fewer than 5 data points (not enough history).
    """
    history = get_atm_iv_history(db_path, symbol, days)
    if len(history) < 5:
        return None
    ivs    = [h["iv"] for h in history if h["iv"] is not None]
    iv_low = min(ivs)
    iv_hi  = max(ivs)
    if iv_hi == iv_low:
        return 50.0  # flat history — indeterminate, return midpoint
    return round((current_iv - iv_low) / (iv_hi - iv_low) * 100, 2)
