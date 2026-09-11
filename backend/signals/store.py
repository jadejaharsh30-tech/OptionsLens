# optionslens/backend/signals/store.py
"""
Persistence for signal evaluations.

Every evaluation is written, fired or not. This is not bookkeeping pedantry:

- The non-fires are the denominator. A hit rate computed only over fires is not
  a hit rate, and "how often did the setup nearly trigger" is usually the more
  informative question.
- `reason` separates kinds of silence. "IV rank 42 below threshold 80" means the
  signal is working and the market is quiet; "insufficient history" means the
  signal has not run at all. Without the log those look identical from outside.
- Storing the continuous `strength` alongside the binary `fired` lets the
  threshold be swept after the fact instead of re-running every evaluation.
"""
import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional

from config import MARKET_DATA_DB
from signals.base import Direction, Signal, SignalResult


@contextmanager
def _conn(db_path: str):
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str = MARKET_DATA_DB):
    """Lives alongside the recorded chain data so research reads one file."""
    with _conn(db_path) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS signal_evaluations (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id       TEXT,
                signal_id    TEXT NOT NULL,
                version      INTEGER NOT NULL,
                ts           TEXT NOT NULL,
                session_date TEXT NOT NULL,
                symbol       TEXT NOT NULL,
                fired        INTEGER NOT NULL,
                direction    TEXT,
                strength     REAL,
                reason       TEXT,
                features     TEXT,
                params       TEXT,
                UNIQUE(run_id, signal_id, version, ts, symbol)
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_eval_lookup
            ON signal_evaluations(signal_id, version, symbol, ts)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_eval_fired
            ON signal_evaluations(signal_id, fired, session_date)
        """)


def _session_date(ts: str) -> str:
    return ts[:10]


def write_evaluation(result: SignalResult, run_id: Optional[str] = None,
                     params: Optional[dict] = None,
                     db_path: str = MARKET_DATA_DB) -> int:
    """Persist one evaluation. Idempotent per (run_id, signal, ts, symbol)."""
    with _conn(db_path) as conn:
        cur = conn.execute("""
            INSERT OR IGNORE INTO signal_evaluations
                (run_id, signal_id, version, ts, session_date, symbol,
                 fired, direction, strength, reason, features, params)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id, result.signal_id, result.version, result.ts,
            _session_date(result.ts), result.symbol,
            1 if result.fired else 0,
            result.signal.direction.value if result.signal else None,
            result.signal.strength if result.signal else None,
            result.reason,
            json.dumps(_jsonable(result.features)),
            json.dumps(params or {}),
        ))
        return cur.rowcount


def write_evaluations(results: list[SignalResult], run_id: Optional[str] = None,
                      params: Optional[dict] = None,
                      db_path: str = MARKET_DATA_DB) -> int:
    return sum(write_evaluation(r, run_id, params, db_path) for r in results)


def _jsonable(obj):
    """Features may hold None or floats; keep serialisation total and lossless."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return str(obj)


def iter_evaluations(signal_id: str, version: Optional[int] = None,
                     symbol: Optional[str] = None,
                     fired_only: bool = False,
                     run_id: Optional[str] = None,
                     db_path: str = MARKET_DATA_DB) -> Iterator[dict]:
    """Replay stored evaluations in time order."""
    clauses = ["signal_id = ?"]
    args: list = [signal_id]
    if version is not None:
        clauses.append("version = ?"); args.append(version)
    if symbol:
        clauses.append("symbol = ?"); args.append(symbol)
    if fired_only:
        clauses.append("fired = 1")
    if run_id:
        clauses.append("run_id = ?"); args.append(run_id)

    with _conn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"""
            SELECT * FROM signal_evaluations
            WHERE {' AND '.join(clauses)}
            ORDER BY ts ASC
        """, args).fetchall()

    for r in rows:
        d = dict(r)
        d["features"] = json.loads(d["features"] or "{}")
        d["params"]   = json.loads(d["params"] or "{}")
        d["fired"]    = bool(d["fired"])
        yield d


def evaluation_summary(signal_id: str, version: Optional[int] = None,
                       db_path: str = MARKET_DATA_DB) -> dict:
    """
    Fire rate and the distribution of non-fire reasons.

    The reason breakdown is the useful half: a signal that never fires because
    it is always warming up is broken, while one that never fires because the
    threshold is never met is merely selective.
    """
    clauses = ["signal_id = ?"]
    args: list = [signal_id]
    if version is not None:
        clauses.append("version = ?"); args.append(version)
    where = " AND ".join(clauses)

    with _conn(db_path) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM signal_evaluations WHERE {where}", args
        ).fetchone()[0]
        fired = conn.execute(
            f"SELECT COUNT(*) FROM signal_evaluations WHERE {where} AND fired = 1", args
        ).fetchone()[0]
        reasons = conn.execute(f"""
            SELECT reason, COUNT(*) FROM signal_evaluations
            WHERE {where} AND fired = 0
            GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 20
        """, args).fetchall()
        by_dir = conn.execute(f"""
            SELECT direction, COUNT(*) FROM signal_evaluations
            WHERE {where} AND fired = 1
            GROUP BY direction
        """, args).fetchall()

    return {
        "signal_id":     signal_id,
        "version":       version,
        "evaluations":   total,
        "fired":         fired,
        "fire_rate_pct": round(fired / total * 100, 2) if total else 0.0,
        "skip_reasons":  [{"reason": r[0], "count": r[1]} for r in reasons],
        "by_direction":  {r[0]: r[1] for r in by_dir},
    }
