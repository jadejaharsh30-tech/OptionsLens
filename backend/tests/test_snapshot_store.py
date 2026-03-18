import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Use isolated test DB — never touches production DB
TEST_DB = "/tmp/test_optionslens.db"

# Patch DB_PATH before import
import config
config.DB_PATH = TEST_DB

from snapshot_store import (
    init_db, write_iv_snapshot, write_atm_iv, get_atm_iv_history, get_iv_rank,
    write_spot_price, get_spot_history,
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

def test_iv_rank_returns_none_with_no_history():
    setup()
    result = get_iv_rank(TEST_DB, "NIFTY", current_iv=0.15, days=252)
    assert result is None
    teardown()

def test_iv_rank_returns_none_with_insufficient_history():
    setup()
    for i in range(3):
        write_atm_iv(TEST_DB, f"2025-01-{i+1:02d}", "NIFTY", "23-01-2025", 0.10 + i * 0.01)
    result = get_iv_rank(TEST_DB, "NIFTY", current_iv=0.12, days=252)
    assert result is None
    teardown()

def test_iv_rank_calculation():
    setup()
    for i in range(10):
        write_atm_iv(TEST_DB, f"2025-01-{i+1:02d}", "NIFTY", "23-01-2025", 0.10 + i * 0.01)
    rank = get_iv_rank(TEST_DB, "NIFTY", current_iv=0.14, days=252)
    assert rank is not None
    assert abs(rank - 44.44) < 0.1, f"Expected ~44.44, got {rank}"
    teardown()

def test_iv_rank_at_minimum_is_zero():
    setup()
    for i in range(10):
        write_atm_iv(TEST_DB, f"2025-01-{i+1:02d}", "NIFTY", "23-01-2025", 0.10 + i * 0.01)
    rank = get_iv_rank(TEST_DB, "NIFTY", current_iv=0.10, days=252)
    assert rank == 0.0
    teardown()

def test_iv_rank_at_maximum_is_100():
    setup()
    for i in range(10):
        write_atm_iv(TEST_DB, f"2025-01-{i+1:02d}", "NIFTY", "23-01-2025", 0.10 + i * 0.01)
    rank = get_iv_rank(TEST_DB, "NIFTY", current_iv=0.19, days=252)
    assert rank == 100.0
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

def test_spot_history_duplicate_ignored():
    setup()
    write_spot_price(TEST_DB, "2025-01-15", "NIFTY", 22500.5)
    write_spot_price(TEST_DB, "2025-01-15", "NIFTY", 99999.0)   # duplicate date
    history = get_spot_history(TEST_DB, "NIFTY", days=30)
    assert len(history) == 1
    assert abs(history[0]["spot"] - 22500.5) < 1e-3   # first write wins
    teardown()
