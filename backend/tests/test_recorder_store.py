"""
Recorder store tests.

The property that matters most: a snapshot written by the live recorder and a
snapshot replayed from disk must be identical, because that equivalence is what
lets one signal implementation serve both live trading and backtesting.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_hours import SessionPhase  # noqa: E402
from recorder.models import ChainRow, ChainSnapshot  # noqa: E402
from recorder.store import (  # noqa: E402
    coverage_stats, init_db, iter_snapshots, recorded_dates,
    write_eod_close, write_snapshot,
)


def make_snapshot(ts="2026-09-11T10:00:00+05:30", symbol="NIFTY", spot=24500.0):
    return ChainSnapshot(
        ts            = ts,
        session_date  = "2026-09-11",
        session_phase = SessionPhase.CONTINUOUS,
        symbol        = symbol,
        expiry_date   = "17-09-2026",
        expiry_epoch  = 1789564800,
        spot          = spot,
        rows          = (
            ChainRow(strike=24500, option_type="CE", oi=1000, ltp=120.5,
                     bid=120.0, ask=121.0, volume=5000),
            ChainRow(strike=24500, option_type="PE", oi=1500, ltp=95.25,
                     bid=95.0, ask=95.5, volume=4200),
            ChainRow(strike=24600, option_type="CE", oi=800, ltp=70.0,
                     bid=69.5, ask=70.5, volume=3100),
        ),
    )


def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    return path


def test_roundtrip_preserves_the_snapshot_exactly():
    db = temp_db()
    try:
        original = make_snapshot()
        assert write_snapshot(original, db) == 3

        replayed = list(iter_snapshots("NIFTY", "2026-09-11", db))
        assert len(replayed) == 1

        got = replayed[0]
        assert got.symbol == original.symbol
        assert got.spot == original.spot
        assert got.session_phase is SessionPhase.CONTINUOUS
        assert got.expiry_date == original.expiry_date
        assert len(got.rows) == 3
        assert got.by_key()[(24500, "CE")].ltp == 120.5
        assert got.by_key()[(24500, "PE")].bid == 95.0
    finally:
        os.unlink(db)


def test_rewriting_the_same_poll_is_idempotent():
    """A recorder restart mid-minute must not duplicate rows."""
    db = temp_db()
    try:
        snap = make_snapshot()
        assert write_snapshot(snap, db) == 3
        assert write_snapshot(snap, db) == 0
        assert len(list(iter_snapshots("NIFTY", "2026-09-11", db))) == 1
    finally:
        os.unlink(db)


def test_replay_is_chronological():
    db = temp_db()
    try:
        for ts in ("2026-09-11T10:02:00+05:30",
                   "2026-09-11T10:00:00+05:30",
                   "2026-09-11T10:01:00+05:30"):
            write_snapshot(make_snapshot(ts=ts), db)

        got = [s.ts for s in iter_snapshots("NIFTY", "2026-09-11", db)]
        assert got == sorted(got)
    finally:
        os.unlink(db)


def test_symbols_are_isolated():
    db = temp_db()
    try:
        write_snapshot(make_snapshot(symbol="NIFTY"), db)
        write_snapshot(make_snapshot(symbol="BANKNIFTY", spot=52000.0), db)

        nifty = list(iter_snapshots("NIFTY", "2026-09-11", db))
        assert len(nifty) == 1 and nifty[0].spot == 24500.0

        stats = coverage_stats(db)
        assert stats["total_snapshots"] == 2
        assert {s["symbol"] for s in stats["symbols"]} == {"NIFTY", "BANKNIFTY"}
    finally:
        os.unlink(db)


def test_eod_close_is_stored_apart_from_intraday_prints():
    """Under CAS the official close is not any intraday LTP."""
    db = temp_db()
    try:
        write_snapshot(make_snapshot(), db)
        write_eod_close("2026-09-11", "NIFTY", 24555.75,
                        "fyers_history", "2026-09-11T15:45:00+05:30", db)

        import sqlite3
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT close_price, source FROM eod_close WHERE symbol='NIFTY'"
        ).fetchone()
        conn.close()
        assert row[0] == 24555.75
        assert row[1] == "fyers_history"
    finally:
        os.unlink(db)


def test_recorded_dates_lists_the_research_universe():
    db = temp_db()
    try:
        write_snapshot(make_snapshot(), db)
        assert recorded_dates("NIFTY", db) == ["2026-09-11"]
        assert recorded_dates(None, db) == ["2026-09-11"]
    finally:
        os.unlink(db)


def test_chain_row_mid_and_spread_handle_empty_books():
    tradeable = ChainRow(strike=24500, option_type="CE", bid=100.0, ask=102.0)
    assert tradeable.mid == 101.0
    assert round(tradeable.spread_pct, 4) == round(2 / 101 * 100, 4)

    # One-sided book: no mid, no spread — signals must not trade these.
    assert ChainRow(strike=24500, option_type="CE", bid=0, ask=102.0).mid is None
    assert ChainRow(strike=24500, option_type="CE", bid=0, ask=0).spread_pct is None
