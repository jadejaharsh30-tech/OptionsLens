# optionslens/backend/lot_sizes.py
"""
Contract lot sizes, read from exchange data rather than a constant.

`config.UNDERLYINGS[...]["lot_size"]` was a hand-maintained number, and by
September 2026 both index entries were wrong against what NSE was publishing:
NIFTY 75 against 65, BANKNIFTY 15 against 30. Every consumer scales by lot
size — trade sizing, notional, GEX — and the BANKNIFTY error made the sizer
take twice the intended risk.

NSE revises lot sizes a few times a year, and during a revision old and new
expiries trade side by side with DIFFERENT lot sizes. So the truth is per
contract, not per symbol: the importer records the lot size of every
(symbol, expiry) it sees in the bhavcopy, and lookups prefer the exact expiry.

Resolution order for `lot_size_for(symbol, expiry)`:
  1. the recorded lot size for that exact expiry
  2. the lot size of the most recently listed expiry for the symbol, which is
     what a newly listed contract will carry unless a revision just landed
  3. config, as a last resort for a symbol never imported

The table is only as fresh as the last import. A revision that lands between
imports is picked up by the next one.
"""
import sqlite3
from datetime import datetime
from typing import Iterable, Optional

from config import DB_PATH, UNDERLYINGS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contract_lot_size (
    symbol      TEXT NOT NULL,
    expiry      TEXT NOT NULL,       -- ISO YYYY-MM-DD, so it sorts
    lot_size    INTEGER NOT NULL,
    first_seen  TEXT NOT NULL,       -- first trading date the contract appeared
    last_seen   TEXT NOT NULL,
    source      TEXT NOT NULL,
    PRIMARY KEY (symbol, expiry)
)
"""


def init_db(db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(_SCHEMA)
    conn.commit()
    conn.close()


def record_lot_sizes(rows: Iterable[tuple[str, str, int, str, str]],
                     db_path: str = DB_PATH, source: str = "bhavcopy") -> int:
    """
    Upsert (symbol, expiry_iso, lot_size, first_seen, last_seen) rows.

    Widens the seen-range on conflict rather than replacing it, so importing
    overlapping date ranges in any order converges on the same table.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(_SCHEMA)
    before = conn.total_changes
    conn.executemany("""
        INSERT INTO contract_lot_size
            (symbol, expiry, lot_size, first_seen, last_seen, source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, expiry) DO UPDATE SET
            lot_size   = excluded.lot_size,
            first_seen = MIN(first_seen, excluded.first_seen),
            last_seen  = MAX(last_seen,  excluded.last_seen)
    """, [(s.upper(), e, int(l), f, la, source) for s, e, l, f, la in rows])
    conn.commit()
    written = conn.total_changes - before
    conn.close()
    return written


def lot_size_for(symbol: str, expiry: Optional[str] = None,
                 db_path: str = DB_PATH) -> int:
    """
    Lot size for a contract. `expiry` may be ISO or Fyers DD-MM-YYYY.

    Never raises for a missing table or an unknown symbol: sizing must degrade
    to the config value, not take the endpoint down.
    """
    symbol = symbol.upper()
    iso = _to_iso(expiry) if expiry else None
    try:
        conn = sqlite3.connect(db_path)
        try:
            if iso:
                row = conn.execute(
                    "SELECT lot_size FROM contract_lot_size "
                    "WHERE symbol = ? AND expiry = ?", (symbol, iso)).fetchone()
                if row:
                    return int(row[0])
            row = conn.execute(
                "SELECT lot_size FROM contract_lot_size WHERE symbol = ? "
                "ORDER BY first_seen DESC, expiry DESC LIMIT 1", (symbol,)).fetchone()
            if row:
                return int(row[0])
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    cfg = UNDERLYINGS.get(symbol)
    return cfg["lot_size"] if cfg else 1


def _to_iso(expiry: str) -> Optional[str]:
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(expiry.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None
