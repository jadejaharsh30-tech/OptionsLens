import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Use isolated test DB — never touches production DB
TEST_DB = os.path.join(tempfile.gettempdir(), "test_optionslens.db")  # /tmp does not exist on Windows

# Patch DB_PATH before import
import config
config.DB_PATH = TEST_DB

import sqlite3
from datetime import date, timedelta

import pytest

from snapshot_store import (
    init_db, write_iv_snapshot, write_atm_iv, write_atm_iv_many,
    get_atm_iv_history, get_cm_iv_history, get_iv_percentile,
    write_spot_price, write_spot_many, get_spot_history,
)


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    init_db(TEST_DB)

def teardown():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


# ── Original tests ────────────────────────────────────────────────────────────

def test_write_and_read_snapshot():
    setup()
    write_iv_snapshot(
        db_path=TEST_DB,
        snapshot_date="2025-01-15",
        symbol="NIFTY",
        expiry_date="23-01-2025",
        strike=22000,
        option_type="CE",
        iv=0.14,
        ltp=150.0,
        oi=50000,
    )
    write_atm_iv(TEST_DB, "2025-01-15", "NIFTY", "23-01-2025", 0.14)
    history = get_atm_iv_history(TEST_DB, "NIFTY", days=90)
    assert len(history) == 1
    assert abs(history[0]["iv"] - 0.14) < 1e-6
    teardown()

def test_duplicate_snapshot_ignored():
    setup()
    for _ in range(3):
        write_atm_iv(TEST_DB, "2025-01-15", "NIFTY", "23-01-2025", 0.14)
    history = get_atm_iv_history(TEST_DB, "NIFTY", days=90)
    assert len(history) == 1, f"Expected 1, got {len(history)}"
    teardown()

def _ddmmyyyy(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def _seed_flat_term(n_days: int, ivs=None, start=date(2025, 1, 1)):
    """n_days of dates, each with a 16-day and a 44-day expiry at the same IV,
    so the 30-day constant-maturity reading equals that IV exactly."""
    rows = []
    for i in range(n_days):
        d = start + timedelta(days=i)
        iv = ivs[i] if ivs else 0.10 + i * 0.001
        for tenor in (16, 44):
            rows.append((d.isoformat(), "NIFTY", _ddmmyyyy(d + timedelta(days=tenor)), iv))
    write_atm_iv_many(TEST_DB, rows, source="test")


def test_percentile_none_with_no_history():
    setup()
    assert get_iv_percentile(TEST_DB, "NIFTY", current_iv=0.15) is None
    teardown()


def test_percentile_none_below_twenty_dates():
    setup()
    _seed_flat_term(19)
    assert get_iv_percentile(TEST_DB, "NIFTY", current_iv=0.12) is None
    teardown()


def test_percentile_at_the_extremes_and_middle():
    setup()
    _seed_flat_term(40)                            # 0.100 .. 0.139
    assert get_iv_percentile(TEST_DB, "NIFTY", 0.05) == pytest.approx(0.0)
    assert get_iv_percentile(TEST_DB, "NIFTY", 0.50) == pytest.approx(100.0)
    assert get_iv_percentile(TEST_DB, "NIFTY", 0.1195) == pytest.approx(50.0)
    teardown()


def test_a_spike_day_does_not_pin_later_readings_to_zero():
    # The old range-based IV Rank put 0.12 at ~2.5 here: one 0.90 day set the
    # ceiling for the whole window. Percentile rank puts it at the top.
    setup()
    _seed_flat_term(30, ivs=[0.10] * 29 + [0.90])
    assert get_iv_percentile(TEST_DB, "NIFTY", 0.12) > 90.0
    teardown()


def test_cm_history_does_not_mix_tenors():
    # Regression: one date with 5-day, 30-day and 100-day expiries used to come
    # back as three separate "history" points, 0.08, 0.12 and 0.20.
    setup()
    d = date(2025, 3, 3)
    write_atm_iv_many(TEST_DB, [
        (d.isoformat(), "NIFTY", _ddmmyyyy(d + timedelta(days=5)),   0.08),
        (d.isoformat(), "NIFTY", _ddmmyyyy(d + timedelta(days=30)),  0.12),
        (d.isoformat(), "NIFTY", _ddmmyyyy(d + timedelta(days=100)), 0.20),
    ], source="test")
    cm = get_cm_iv_history(TEST_DB, "NIFTY")
    assert len(cm) == 1
    assert cm[0]["iv"] == pytest.approx(0.12, abs=2e-3)
    teardown()


def test_window_counts_dates_not_rows():
    # Regression: `days` was a row LIMIT, so two expiries per date halved it.
    setup()
    _seed_flat_term(30)
    assert len({r["date"] for r in get_atm_iv_history(TEST_DB, "NIFTY", days=25)}) == 25
    assert len(get_cm_iv_history(TEST_DB, "NIFTY", days=25)) == 25
    teardown()


def test_cm_history_is_most_recent_first():
    setup()
    _seed_flat_term(5)
    dates = [r["date"] for r in get_cm_iv_history(TEST_DB, "NIFTY")]
    assert dates == sorted(dates, reverse=True)
    teardown()


def test_date_without_a_reachable_tenor_is_omitted():
    setup()
    d = date(2025, 3, 3)
    write_atm_iv(TEST_DB, d.isoformat(), "NIFTY", _ddmmyyyy(d + timedelta(days=4)), 0.11)
    assert get_cm_iv_history(TEST_DB, "NIFTY") == []
    teardown()


def test_existing_rows_are_never_overwritten_and_source_is_kept():
    setup()
    write_atm_iv(TEST_DB, "2025-01-15", "NIFTY", "23-01-2025", 0.14)
    added = write_atm_iv_many(TEST_DB, [("2025-01-15", "NIFTY", "23-01-2025", 0.99),
                                        ("2025-01-15", "NIFTY", "30-01-2025", 0.15)],
                              source="bhavcopy")
    assert added == 1
    conn = sqlite3.connect(TEST_DB)
    got = dict(conn.execute("SELECT expiry_date, source FROM atm_iv_history").fetchall())
    iv = conn.execute("SELECT iv FROM atm_iv_history WHERE expiry_date='23-01-2025'").fetchone()[0]
    conn.close()
    assert got == {"23-01-2025": "live", "30-01-2025": "bhavcopy"}
    assert iv == pytest.approx(0.14)
    teardown()


def test_init_adds_the_source_column_to_an_old_database():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    conn = sqlite3.connect(TEST_DB)
    conn.execute("""CREATE TABLE atm_iv_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_date TEXT NOT NULL,
        symbol TEXT NOT NULL, expiry_date TEXT NOT NULL, iv REAL,
        UNIQUE(snapshot_date, symbol, expiry_date))""")
    conn.execute("INSERT INTO atm_iv_history (snapshot_date, symbol, expiry_date, iv) "
                 "VALUES ('2025-01-02', 'NIFTY', '23-01-2025', 0.13)")
    conn.commit(); conn.close()
    init_db(TEST_DB)
    init_db(TEST_DB)                                     # idempotent
    rows = get_atm_iv_history(TEST_DB, "NIFTY")
    assert rows[0]["iv"] == pytest.approx(0.13)
    teardown()


def test_different_symbols_isolated():
    setup()
    write_atm_iv(TEST_DB, "2025-01-01", "NIFTY",     "23-01-2025", 0.14)
    write_atm_iv(TEST_DB, "2025-01-01", "BANKNIFTY",  "23-01-2025", 0.18)
    nifty_hist = get_atm_iv_history(TEST_DB, "NIFTY",     days=252)
    bnf_hist   = get_atm_iv_history(TEST_DB, "BANKNIFTY",  days=252)
    assert len(nifty_hist) == 1
    assert len(bnf_hist)   == 1
    assert abs(nifty_hist[0]["iv"] - 0.14) < 1e-6
    assert abs(bnf_hist[0]["iv"]   - 0.18) < 1e-6
    teardown()


# ── New spot_history tests ────────────────────────────────────────────────────

def test_write_and_read_spot_history():
    setup()
    write_spot_price(TEST_DB, "2025-01-15", "NIFTY", 22500.5)
    write_spot_price(TEST_DB, "2025-01-16", "NIFTY", 22600.0)
    history = get_spot_history(TEST_DB, "NIFTY", days=30)
    assert len(history) == 2
    # Most recent first
    assert abs(history[0]["spot"] - 22600.0) < 1e-3
    assert abs(history[1]["spot"] - 22500.5) < 1e-3
    teardown()

def test_bulk_spot_write_never_overwrites():
    setup()
    write_spot_price(TEST_DB, "2025-01-15", "NIFTY", 22500.5)
    added = write_spot_many(TEST_DB, [("2025-01-15", "NIFTY", 1.0),
                                      ("2025-01-16", "NIFTY", 22600.0)])
    assert added == 1
    assert get_spot_history(TEST_DB, "NIFTY")[1]["spot"] == pytest.approx(22500.5)
    teardown()

def test_spot_history_duplicate_ignored():
    setup()
    write_spot_price(TEST_DB, "2025-01-15", "NIFTY", 22500.5)
    write_spot_price(TEST_DB, "2025-01-15", "NIFTY", 99999.0)   # duplicate date
    history = get_spot_history(TEST_DB, "NIFTY", days=30)
    assert len(history) == 1
    assert abs(history[0]["spot"] - 22500.5) < 1e-3   # first write wins
    teardown()
