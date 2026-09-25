"""
Tests for the bhavcopy -> ChainSnapshot adapter.

Built on a synthetic archive whose option prices come from Black-76, so a
snapshot that survives the adapter must also solve for the volatility it was
generated with. That end-to-end check is stronger than asserting field-by-field
mapping: it proves the rebuilt chain is priceable by the same engines the live
path uses.

The data traps are each pinned by a test, because every one of them was
measured on real files and would otherwise be re-learned the hard way.
"""
import math
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bhavcopy.snapshots import (  # noqa: E402
    _oi_change_pct, available_dates, iter_eod_snapshots, materialize,
)
from chain_pricing import atm_iv_for_chain, price_for_iv  # noqa: E402
from iv_engine import black76_price  # noqa: E402
from market_hours import SessionPhase, time_to_expiry  # noqa: E402

R, SIGMA = 0.065, 0.16

ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_option (
    trad_dt TEXT NOT NULL, symbol TEXT NOT NULL, expiry_dt TEXT NOT NULL,
    actl_expiry_dt TEXT, strike REAL NOT NULL, option_type TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, last REAL, prev_close REAL,
    settle REAL, underlying REAL, oi REAL, chg_oi REAL, volume REAL,
    num_trades REAL, turnover_raw REAL, lot_size REAL, instrument TEXT,
    era TEXT NOT NULL,
    PRIMARY KEY (trad_dt, symbol, expiry_dt, strike, option_type)
);
"""


def make_archive(dates=("2026-09-21", "2026-09-22", "2026-09-23"),
                 expiry="2026-10-29", spot=24500.0, volume=500.0,
                 with_underlying=True, symbol="NIFTY") -> str:
    """A small archive with Black-76-consistent closes."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(ARCHIVE_SCHEMA)

    rows = []
    for di, d in enumerate(dates):
        s = spot + di * 10
        T = time_to_expiry(
            __import__("datetime").datetime.strptime(expiry, "%Y-%m-%d")
            .strftime("%d-%m-%Y"),
            now=__import__("datetime").datetime.fromisoformat(f"{d}T15:30:00+05:30"),
        )
        fwd = s * math.exp(R * max(T, 1e-6))
        for k in (24100.0, 24200.0, 24300.0, 24400.0, 24500.0,
                  24600.0, 24700.0, 24800.0, 24900.0):
            for opt in ("CE", "PE"):
                px = black76_price(fwd, k, max(T, 1e-6), R, SIGMA, opt)
                # Realistic open-interest shape: calls build above the money,
                # puts below. Flat OI leaves cumulative gamma never crossing
                # zero, so there is no flip level and GEX signals cannot run.
                skew = (max(0.0, k - s) if opt == "CE" else max(0.0, s - k)) / 10.0
                oi = 1000.0 + di * 100 + skew * 20.0
                rows.append((
                    d, symbol, expiry, expiry, k, opt,
                    None, None, None, round(px, 2), None, None, None,
                    s if with_underlying else None,
                    oi, 100.0, volume, None, None, 65.0,
                    "OPTIDX", "udiff",
                ))
    conn.executemany(
        "INSERT INTO daily_option VALUES (" + ",".join("?" * 22) + ")", rows)
    conn.commit()
    conn.close()
    return path


def temp_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


# ── Shape ─────────────────────────────────────────────────────────────────────

def test_rebuilds_a_snapshot_per_expiry():
    src = make_archive()
    try:
        snaps = list(iter_eod_snapshots("NIFTY", "2026-09-21", src))
        assert len(snaps) == 1
        s = snaps[0]
        assert s.symbol == "NIFTY"
        assert s.session_date == "2026-09-21"
        assert s.expiry_date == "29-10-2026"        # Fyers DD-MM-YYYY
        assert len(s.rows) == 18
        assert s.spot == 24500.0
    finally:
        os.unlink(src)


def test_tagged_end_of_day_so_intraday_signals_can_refuse_it():
    """A daily close must never be mistaken for a live quote."""
    src = make_archive()
    try:
        s = next(iter_eod_snapshots("NIFTY", "2026-09-21", src))
        assert s.session_phase is SessionPhase.END_OF_DAY
    finally:
        os.unlink(src)


def test_timestamp_is_the_session_close():
    src = make_archive()
    try:
        s = next(iter_eod_snapshots("NIFTY", "2026-09-21", src))
        assert s.ts.startswith("2026-09-21T15:30:00")
        assert "+05:30" in s.ts
    finally:
        os.unlink(src)


def test_no_book_so_price_falls_back_to_the_close():
    src = make_archive()
    try:
        s = next(iter_eod_snapshots("NIFTY", "2026-09-21", src))
        r = s.by_key()[(24500.0, "CE")]
        assert r.bid == 0.0 and r.ask == 0.0
        assert r.mid is None                       # nothing invented
        assert price_for_iv({"ltp": r.ltp, "bid": r.bid, "ask": r.ask}) == r.ltp
    finally:
        os.unlink(src)


# ── The rebuilt chain must actually price ─────────────────────────────────────

def test_rebuilt_chain_solves_back_to_the_generating_vol():
    """The end-to-end check: adapter output is priceable by the live engines."""
    src = make_archive()
    try:
        s = next(iter_eod_snapshots("NIFTY", "2026-09-21", src))
        chain = [{"strike": r.strike, "option_type": r.option_type,
                  "ltp": r.ltp, "bid": r.bid, "ask": r.ask} for r in s.rows]
        T = time_to_expiry(s.expiry_date,
                           now=__import__("datetime").datetime.fromisoformat(s.ts))
        iv = atm_iv_for_chain(chain, T, s.spot)
        assert iv is not None
        assert abs(iv - SIGMA) < 0.01              # within a vol point
    finally:
        os.unlink(src)


# ── Data traps ────────────────────────────────────────────────────────────────

def test_thin_prints_are_dropped():
    """An untraded contract republishes yesterday's close — not a current price."""
    src = make_archive(volume=5.0)
    try:
        assert list(iter_eod_snapshots("NIFTY", "2026-09-21", src,
                                       min_volume=25.0)) == []
        assert list(iter_eod_snapshots("NIFTY", "2026-09-21", src,
                                       min_volume=1.0)) != []
    finally:
        os.unlink(src)


def test_expiring_contracts_are_excluded():
    """On expiry day the settlement column is the index level, not a premium."""
    src = make_archive(dates=("2026-10-29",), expiry="2026-10-29")
    try:
        assert list(iter_eod_snapshots("NIFTY", "2026-10-29", src)) == []
    finally:
        os.unlink(src)


def test_dates_without_an_underlying_price_are_skipped_not_guessed():
    """Pre-2024-07-08 files publish no spot; rebuilding it is not done yet."""
    src = make_archive(with_underlying=False)
    try:
        assert list(iter_eod_snapshots("NIFTY", "2026-09-21", src)) == []
    finally:
        os.unlink(src)


def test_oi_change_pct_refuses_a_zero_base():
    """
    A huge percentage off a near-zero prior level is the artefact that makes the
    live SHORT_BUILDUP rule fire on +2041% readings. Returns 0.0 instead.
    """
    assert _oi_change_pct(1100.0, 100.0) == 10.0
    assert _oi_change_pct(100.0, 100.0) == 0.0     # prior level was zero
    assert _oi_change_pct(50.0, 100.0) == 0.0      # prior level negative
    assert _oi_change_pct(None, 100.0) == 0.0


def test_missing_archive_returns_empty_rather_than_raising():
    assert list(iter_eod_snapshots("NIFTY", "2026-09-21", "/nonexistent.db")) == []
    assert available_dates("NIFTY", "/nonexistent.db") == []


# ── Materialisation and replay ────────────────────────────────────────────────

def test_materialize_writes_a_replayable_store():
    from recorder.store import iter_snapshots, recorded_dates

    src, dest = make_archive(), temp_path()
    try:
        stats = materialize("NIFTY", dest, src_db=src)
        assert stats["snapshots"] == 3
        assert stats["rows"] == 54

        assert recorded_dates("NIFTY", dest) == ["2026-09-21", "2026-09-22",
                                                 "2026-09-23"]
        replayed = list(iter_snapshots("NIFTY", "2026-09-21", dest))
        assert len(replayed) == 1
        assert replayed[0].session_phase is SessionPhase.END_OF_DAY
        assert len(replayed[0].rows) == 18
    finally:
        os.unlink(src)
        if os.path.exists(dest):
            os.unlink(dest)


def test_materialize_is_idempotent():
    src, dest = make_archive(), temp_path()
    try:
        first = materialize("NIFTY", dest, src_db=src)
        second = materialize("NIFTY", dest, src_db=src)
        assert first["rows"] == 54
        assert second["rows"] == 0          # INSERT OR IGNORE, nothing doubled
    finally:
        os.unlink(src)
        if os.path.exists(dest):
            os.unlink(dest)


def test_nearest_expiry_only_is_the_default():
    src, dest = make_archive(), temp_path()
    try:
        conn = sqlite3.connect(src)
        conn.execute("""
            INSERT INTO daily_option (trad_dt, symbol, expiry_dt, strike,
                option_type, close, underlying, oi, chg_oi, volume, era)
            VALUES ('2026-09-21','NIFTY','2026-11-26',24500.0,'CE',
                    300.0,24500.0,1000.0,100.0,500.0,'udiff')
        """)
        conn.commit()
        conn.close()

        assert len(list(iter_eod_snapshots("NIFTY", "2026-09-21", src))) == 2
        stats = materialize("NIFTY", dest, dates=["2026-09-21"], src_db=src)
        assert stats["snapshots"] == 1        # nearest only

        os.unlink(dest)
        stats_all = materialize("NIFTY", dest, dates=["2026-09-21"], src_db=src,
                                nearest_expiry_only=False)
        assert stats_all["snapshots"] == 2
    finally:
        os.unlink(src)
        if os.path.exists(dest):
            os.unlink(dest)


def test_backtester_runs_over_materialised_eod_history():
    """
    The whole point: EOD history flows through the same backtester, with the
    same null comparison, as recorder data.
    """
    from backtest.engine import run_backtest
    from signals.store import init_db as init_sig
    import signals.library  # noqa: F401

    dates = [f"2026-09-{d:02d}" for d in range(1, 25)]
    src, dest = make_archive(dates=tuple(dates)), temp_path()
    try:
        materialize("NIFTY", dest, src_db=src)
        init_sig(dest)

        run = run_backtest("gex_regime", "NIFTY", db_path=dest)
        out = run.to_dict()
        assert out["sessions"] == len(dates)
        assert out["evaluations"] == len(dates)   # one EOD bar per session
        assert out["signals_fired"] > 0
        # One bar per session, so the engine must pick DAILY labelling: intraday
        # horizons would all score n=0 and the EOD label would compare a bar
        # with itself.
        assert out["label_mode"] == "daily"
        assert set(out["horizons"]) == {"1d", "5d", "20d"}
        # Cross-session labels are real numbers, not the degenerate self-compare.
        assert out["horizons"]["1d"]["signal"]["n"] > 0
        assert out["horizons"]["1d"]["verdict"] in (
            "EDGE", "INVERSE_EDGE", "NO_EDGE", "INSUFFICIENT_DATA", "INDETERMINATE")
    finally:
        os.unlink(src)
        if os.path.exists(dest):
            os.unlink(dest)
