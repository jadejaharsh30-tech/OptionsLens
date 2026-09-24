import sys, os, sqlite3, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TEST_DB = os.path.join(tempfile.gettempdir(), "test_percentile.db")  # /tmp does not exist on Windows


def _init_test_db(db_path):
    """Minimal schema — only the oi_snapshots table needed for percentile tests."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oi_snapshots (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp      TEXT NOT NULL,
            session_date   TEXT NOT NULL,
            symbol         TEXT NOT NULL DEFAULT 'NIFTY',
            expiry_date    TEXT,
            strike         REAL,
            option_type    TEXT,
            oi             REAL,
            oi_change      REAL,
            oi_change_pct  REAL,
            prev_oi        REAL,
            ltp            REAL,
            volume         REAL
        )
    """)
    conn.commit()
    conn.close()


def _seed(db_path, symbol, strike, option_type, values):
    """Helper: write fake oi_snapshots rows with given oi_change_pct values."""
    from datetime import datetime, timedelta
    conn = sqlite3.connect(db_path)
    base = datetime(2025, 1, 1, 10, 0, 0)
    for i, pct in enumerate(values):
        ts = (base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT INTO oi_snapshots
                (timestamp, session_date, symbol, expiry_date,
                 strike, option_type, oi, oi_change, oi_change_pct, prev_oi, ltp, volume)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (ts, "2025-01-01", symbol, "30-01-2025",
              strike, option_type, 1000, pct * 10, pct, 1000 - pct * 10, 50.0, 200))
    conn.commit()
    conn.close()


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    _init_test_db(TEST_DB)


def teardown():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


from alert_engine.percentile_threshold import get_oi_pct_percentile, get_adaptive_threshold


def test_percentile_returns_none_insufficient_data():
    setup()
    result = get_oi_pct_percentile(TEST_DB, "NIFTY", "CE", 90.0, min_samples=50)
    assert result is None
    teardown()


def test_percentile_calculation():
    setup()
    # 100 values: 1 to 100 — 90th percentile should be ~90
    _seed(TEST_DB, "NIFTY", 22000, "CE", list(range(1, 101)))
    p90 = get_oi_pct_percentile(TEST_DB, "NIFTY", "CE", 90.0, min_samples=50)
    assert p90 is not None
    assert 85 <= p90 <= 95, f"Expected ~90, got {p90}"
    teardown()


def test_adaptive_threshold_falls_back_to_static():
    setup()
    # Not enough data — should return the static fallback
    threshold = get_adaptive_threshold(TEST_DB, "NIFTY", "CE", 90.0, 50, 500.0)
    assert threshold == 500.0
    teardown()


def test_adaptive_threshold_uses_percentile_when_sufficient():
    setup()
    _seed(TEST_DB, "NIFTY", 22000, "CE", list(range(1, 101)))
    threshold = get_adaptive_threshold(TEST_DB, "NIFTY", "CE", 90.0, 50, 500.0)
    # Should use computed percentile (~90), not the static 500
    assert threshold < 500.0 and threshold > 0
    teardown()
