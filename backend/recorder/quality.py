# optionslens/backend/recorder/quality.py
"""
Data-quality monitoring for the chain recorder.

The failure that actually costs you research data is not a crash — a crash is
loud. It is a recorder that quietly stops, or records half a session, and is
only noticed weeks later when the gap is unrecoverable. This module makes
coverage measurable so silence can be distinguished from health.

Everything here reads the recorded store only; nothing touches Fyers.
"""
from datetime import datetime, timedelta
from typing import Optional

from config import MARKET_DATA_DB
from market_hours import (
    CONTINUOUS_OPEN, DERIVATIVES_CLOSE, IST, is_trading_day,
)
from recorder.store import _conn

# A poll is "on time" if it lands within this fraction of the interval.
GAP_TOLERANCE = 1.5


def expected_snapshot_count(interval_sec: int = 60) -> int:
    """
    Polls a complete session should produce, 09:15 to 15:40 inclusive.

    Recording deliberately runs through the CAS window and the post-auction
    derivatives tail, so the denominator covers the full 385 minutes rather
    than stopping at the old 15:30 cash close.
    """
    open_sec  = CONTINUOUS_OPEN.hour * 3600 + CONTINUOUS_OPEN.minute * 60
    close_sec = DERIVATIVES_CLOSE.hour * 3600 + DERIVATIVES_CLOSE.minute * 60
    return max(1, (close_sec - open_sec) // interval_sec)


def find_gaps(timestamps: list[str], interval_sec: int = 60) -> list[dict]:
    """
    Locate missing stretches in a day's timestamps.

    Returns one entry per gap with its bounds and how many polls were lost.
    """
    if len(timestamps) < 2:
        return []

    parsed = sorted(datetime.fromisoformat(ts) for ts in timestamps)
    threshold = timedelta(seconds=interval_sec * GAP_TOLERANCE)

    gaps = []
    for prev, curr in zip(parsed, parsed[1:]):
        delta = curr - prev
        if delta > threshold:
            gaps.append({
                "from":           prev.isoformat(),
                "to":             curr.isoformat(),
                "seconds":        int(delta.total_seconds()),
                "missed_polls":   int(delta.total_seconds() // interval_sec) - 1,
            })
    return gaps


def day_coverage(symbol: str, session_date: str, interval_sec: int = 60,
                 db_path: str = MARKET_DATA_DB) -> dict:
    """
    Coverage report for one symbol on one session date.

    `completeness_pct` is against a full session, so a day the recorder was
    started late shows as partial rather than as healthy.
    """
    with _conn(db_path) as conn:
        rows = conn.execute("""
            SELECT ts, session_phase, row_count FROM chain_meta
            WHERE symbol = ? AND session_date = ?
            ORDER BY ts ASC
        """, (symbol, session_date)).fetchall()

    if not rows:
        return {
            "symbol": symbol, "session_date": session_date,
            "snapshots": 0, "expected": expected_snapshot_count(interval_sec),
            "completeness_pct": 0.0, "first_ts": None, "last_ts": None,
            "gaps": [], "largest_gap_sec": 0, "phases": {}, "total_rows": 0,
            "status": "MISSING",
        }

    timestamps = [r[0] for r in rows]
    expected   = expected_snapshot_count(interval_sec)
    gaps       = find_gaps(timestamps, interval_sec)

    phases: dict[str, int] = {}
    for _, phase, _ in rows:
        phases[phase] = phases.get(phase, 0) + 1

    completeness = min(100.0, len(rows) / expected * 100.0)

    # A day missing the CAS window is not a complete day even if the count
    # looks healthy — that window is the part we most want to study.
    has_cas = phases.get("CAS_WINDOW", 0) > 0

    if completeness >= 95 and not gaps:
        status = "COMPLETE"
    elif completeness >= 80:
        status = "PARTIAL"
    else:
        status = "SPARSE"

    return {
        "symbol":           symbol,
        "session_date":     session_date,
        "snapshots":        len(rows),
        "expected":         expected,
        "completeness_pct": round(completeness, 1),
        "first_ts":         timestamps[0],
        "last_ts":          timestamps[-1],
        "gaps":             gaps,
        "largest_gap_sec":  max((g["seconds"] for g in gaps), default=0),
        "phases":           phases,
        "captured_cas_window": has_cas,
        "total_rows":       sum(r[2] or 0 for r in rows),
        "status":           status,
    }


def missing_trading_days(symbol: str, db_path: str = MARKET_DATA_DB) -> list[str]:
    """
    Trading days between our first and last recording with no data at all.

    Only looks inside the recorded range — days before we started are not
    "missing", they never existed for us.
    """
    with _conn(db_path) as conn:
        rows = conn.execute("""
            SELECT DISTINCT session_date FROM chain_meta
            WHERE symbol = ? ORDER BY session_date ASC
        """, (symbol,)).fetchall()

    have = {r[0] for r in rows}
    if len(have) < 2:
        return []

    start = datetime.fromisoformat(min(have)).date()
    end   = datetime.fromisoformat(max(have)).date()

    missing, day = [], start
    while day <= end:
        if is_trading_day(day) and day.isoformat() not in have:
            missing.append(day.isoformat())
        day += timedelta(days=1)
    return missing


def quality_report(symbols: Optional[list[str]] = None, interval_sec: int = 60,
                   db_path: str = MARKET_DATA_DB) -> dict:
    """
    Dataset-wide health summary — the one call the UI and the daily digest use.
    """
    with _conn(db_path) as conn:
        if symbols:
            placeholders = ",".join("?" * len(symbols))
            pairs = conn.execute(f"""
                SELECT DISTINCT symbol, session_date FROM chain_meta
                WHERE symbol IN ({placeholders})
                ORDER BY session_date DESC, symbol ASC
            """, symbols).fetchall()
        else:
            pairs = conn.execute("""
                SELECT DISTINCT symbol, session_date FROM chain_meta
                ORDER BY session_date DESC, symbol ASC
            """).fetchall()

    days = [day_coverage(sym, dt, interval_sec, db_path) for sym, dt in pairs]

    by_symbol: dict[str, list[dict]] = {}
    for d in days:
        by_symbol.setdefault(d["symbol"], []).append(d)

    summary = []
    for sym, entries in sorted(by_symbol.items()):
        complete = sum(1 for e in entries if e["status"] == "COMPLETE")
        summary.append({
            "symbol":          sym,
            "days_recorded":   len(entries),
            "days_complete":   complete,
            "days_missing":    missing_trading_days(sym, db_path),
            "avg_completeness": round(
                sum(e["completeness_pct"] for e in entries) / len(entries), 1
            ),
            "total_rows":      sum(e["total_rows"] for e in entries),
        })

    return {
        "generated_at":  datetime.now(IST).isoformat(),
        "interval_sec":  interval_sec,
        "expected_per_day": expected_snapshot_count(interval_sec),
        "symbols":       summary,
        "recent_days":   days[:20],
    }
