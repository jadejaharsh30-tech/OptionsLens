# optionslens/backend/recorder/store.py
"""
Append-only SQLite store for option-chain snapshots.

THIS STORE NEVER DELETES. The old alert engine pruned its snapshots after 20
minutes; NSE intraday per-strike OI cannot be bought back retroactively, so
every prune was permanent data loss. There is no delete path here on purpose.

Storage cost is not a reason to prune: ~30 strikes x 2 types x 375 minutes is
roughly 2 MB/day/symbol, about 1 GB/year for both indices.

WAL mode is enabled so the backtester can read while the recorder writes.
"""
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional

from config import MARKET_DATA_DB
from market_hours import SessionPhase
from recorder.models import ChainRow, ChainSnapshot


@contextmanager
def _conn(db_path: str):
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")     # concurrent read during writes
        conn.execute("PRAGMA synchronous=NORMAL")   # durable enough, much faster
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str = MARKET_DATA_DB):
    """Create tables and indices. Safe to call on every startup."""
    with _conn(db_path) as conn:
        c = conn.cursor()

        # ── Per-contract rows ────────────────────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS chain_rows (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                session_date  TEXT NOT NULL,
                symbol        TEXT NOT NULL,
                expiry_date   TEXT NOT NULL,
                strike        REAL NOT NULL,
                option_type   TEXT NOT NULL,
                oi            REAL,
                oi_change     REAL,
                oi_change_pct REAL,
                prev_oi       REAL,
                ltp           REAL,
                bid           REAL,
                ask           REAL,
                volume        REAL,
                UNIQUE(ts, symbol, expiry_date, strike, option_type)
            )
        """)

        # ── Per-snapshot header: spot, futures, session phase ────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS chain_meta (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                session_date  TEXT NOT NULL,
                session_phase TEXT NOT NULL,
                symbol        TEXT NOT NULL,
                expiry_date   TEXT NOT NULL,
                expiry_epoch  INTEGER NOT NULL,
                spot          REAL,
                futures       REAL,
                row_count     INTEGER,
                UNIQUE(ts, symbol, expiry_date)
            )
        """)

        # ── Official close, captured AFTER the closing auction ───────────────
        # Kept separate from intraday LTP: under CAS an F&O-eligible stock has
        # no continuous trading from 15:15, so a 15:20 LTP is NOT the close.
        c.execute("""
            CREATE TABLE IF NOT EXISTS eod_close (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_date TEXT NOT NULL,
                symbol       TEXT NOT NULL,
                close_price  REAL NOT NULL,
                source       TEXT,
                captured_at  TEXT,
                UNIQUE(session_date, symbol)
            )
        """)

        # Replay path: "give me symbol X on date D in time order"
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_chain_rows_replay
            ON chain_rows(symbol, session_date, ts)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_chain_meta_replay
            ON chain_meta(symbol, session_date, ts)
        """)
        # Cross-sectional path: "this strike's whole history"
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_chain_rows_strike
            ON chain_rows(symbol, strike, option_type, ts)
        """)


def write_snapshot(snapshot: ChainSnapshot, db_path: str = MARKET_DATA_DB) -> int:
    """
    Persist one snapshot. Returns rows written.

    INSERT OR IGNORE makes re-polling the same aligned timestamp idempotent,
    so a recorder restart mid-minute cannot create duplicates.
    """
    if not snapshot.rows:
        return 0

    with _conn(db_path) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO chain_meta
                (ts, session_date, session_phase, symbol,
                 expiry_date, expiry_epoch, spot, futures, row_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            snapshot.ts, snapshot.session_date, snapshot.session_phase.value,
            snapshot.symbol, snapshot.expiry_date, snapshot.expiry_epoch,
            snapshot.spot, snapshot.futures, len(snapshot.rows),
        ))

        cur = conn.executemany("""
            INSERT OR IGNORE INTO chain_rows
                (ts, session_date, symbol, expiry_date, strike, option_type,
                 oi, oi_change, oi_change_pct, prev_oi, ltp, bid, ask, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            (snapshot.ts, snapshot.session_date, snapshot.symbol,
             snapshot.expiry_date, r.strike, r.option_type,
             r.oi, r.oi_change, r.oi_change_pct, r.prev_oi,
             r.ltp, r.bid, r.ask, r.volume)
            for r in snapshot.rows
        ])
        return cur.rowcount


def write_eod_close(session_date: str, symbol: str, close_price: float,
                    source: str, captured_at: str,
                    db_path: str = MARKET_DATA_DB):
    """Record the official post-auction close, distinct from any intraday print."""
    with _conn(db_path) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO eod_close
                (session_date, symbol, close_price, source, captured_at)
            VALUES (?, ?, ?, ?, ?)
        """, (session_date, symbol, close_price, source, captured_at))


# ── Replay API — this is what the backtester will consume ────────────────────

def iter_snapshots(symbol: str, session_date: str,
                   db_path: str = MARKET_DATA_DB) -> Iterator[ChainSnapshot]:
    """
    Replay one symbol's recorded day in chronological order.

    Yields the exact same ChainSnapshot type the live recorder produces, which
    is what lets one signal implementation serve both runtimes.
    """
    with _conn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        metas = conn.execute("""
            SELECT * FROM chain_meta
            WHERE symbol = ? AND session_date = ?
            ORDER BY ts ASC
        """, (symbol, session_date)).fetchall()

        for m in metas:
            yield _snapshot_from_meta(conn, m)


def _snapshot_from_meta(conn, meta) -> ChainSnapshot:
    """Load one meta row's strikes and assemble the snapshot."""
    rows = conn.execute("""
        SELECT strike, option_type, oi, oi_change, oi_change_pct,
               prev_oi, ltp, bid, ask, volume
        FROM chain_rows
        WHERE ts = ? AND symbol = ? AND expiry_date = ?
        ORDER BY strike ASC, option_type ASC
    """, (meta["ts"], meta["symbol"], meta["expiry_date"])).fetchall()

    return ChainSnapshot(
        ts            = meta["ts"],
        session_date  = meta["session_date"],
        session_phase = SessionPhase(meta["session_phase"]),
        symbol        = meta["symbol"],
        expiry_date   = meta["expiry_date"],
        expiry_epoch  = meta["expiry_epoch"],
        spot          = meta["spot"],
        futures       = meta["futures"],
        rows          = tuple(ChainRow(**dict(r)) for r in rows),
    )


def last_snapshot_of_session(symbol: str, session_date: str,
                             db_path: str = MARKET_DATA_DB,
                             ) -> Optional[ChainSnapshot]:
    """
    The final bar of one session, front expiry, or None.

    Exists so a caller building a DAILY series out of the store does not have to
    walk the session to find its last bar. `iter_snapshots` would load every
    minute of every day and discard all but one — a year of recorder data is
    millions of rows to answer a question about 250 of them.

    Ties on timestamp are broken by the nearest expiry, matching the EOD
    adapter's `nearest_expiry_only`, so a date materialised with the whole term
    structure still yields the front chain rather than an arbitrary one.
    """
    with _conn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        meta = conn.execute("""
            SELECT * FROM chain_meta
            WHERE symbol = ? AND session_date = ?
            ORDER BY ts DESC, expiry_epoch ASC
            LIMIT 1
        """, (symbol, session_date)).fetchone()
        if meta is None:
            return None
        return _snapshot_from_meta(conn, meta)


def recorded_dates(symbol: Optional[str] = None,
                   db_path: str = MARKET_DATA_DB) -> list[str]:
    """Which session dates we hold data for — the research universe so far."""
    with _conn(db_path) as conn:
        if symbol:
            rows = conn.execute("""
                SELECT DISTINCT session_date FROM chain_meta
                WHERE symbol = ? ORDER BY session_date ASC
            """, (symbol,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT DISTINCT session_date FROM chain_meta
                ORDER BY session_date ASC
            """).fetchall()
    return [r[0] for r in rows]


def coverage_stats(db_path: str = MARKET_DATA_DB) -> dict:
    """
    Dataset summary — surfaced in the UI so a silently dead recorder is visible.
    A recorder nobody checks is a recorder that stopped three weeks ago.
    """
    with _conn(db_path) as conn:
        total_rows  = conn.execute("SELECT COUNT(*) FROM chain_rows").fetchone()[0]
        total_snaps = conn.execute("SELECT COUNT(*) FROM chain_meta").fetchone()[0]
        per_symbol  = conn.execute("""
            SELECT symbol,
                   COUNT(DISTINCT session_date) AS days,
                   COUNT(*)                     AS snapshots,
                   MIN(session_date)            AS first_date,
                   MAX(session_date)            AS last_date
            FROM chain_meta GROUP BY symbol ORDER BY symbol
        """).fetchall()

    return {
        "total_rows":      total_rows,
        "total_snapshots": total_snaps,
        "symbols": [
            {"symbol": r[0], "days": r[1], "snapshots": r[2],
             "first_date": r[3], "last_date": r[4]}
            for r in per_symbol
        ],
    }


def session_snapshot_counts(symbol: str, dates: Optional[list[str]] = None,
                            db_path: str = MARKET_DATA_DB) -> dict[str, int]:
    """
    Snapshots held per session date. One query, no row payload.

    Lets a caller tell one-bar-per-session EOD data from the recorder's
    one-per-minute bars before replaying, which decides whether a signal's
    history window should carry across sessions.
    """
    with _conn(db_path) as conn:
        if dates:
            marks = ",".join("?" * len(dates))
            rows = conn.execute(f"""
                SELECT session_date, COUNT(*) FROM chain_meta
                WHERE symbol = ? AND session_date IN ({marks})
                GROUP BY session_date
            """, (symbol, *dates)).fetchall()
        else:
            rows = conn.execute("""
                SELECT session_date, COUNT(*) FROM chain_meta
                WHERE symbol = ? GROUP BY session_date
            """, (symbol,)).fetchall()
    return {r[0]: r[1] for r in rows}
