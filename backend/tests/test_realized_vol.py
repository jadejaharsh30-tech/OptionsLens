"""Tests for realized_vol.py — plain list of floats interface."""
import math
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from realized_vol import compute_realized_vol, compute_rv_series


def test_constant_prices_returns_zero():
    """All same price → zero log returns → RV = 0."""
    closes = [100.0] * 25
    rv = compute_realized_vol(closes, window=20)
    assert rv is not None
    assert abs(rv) < 1e-10


def test_insufficient_data_returns_none():
    """Fewer than window+1 prices → None."""
    closes = [100.0] * 10
    rv = compute_realized_vol(closes, window=20)
    assert rv is None


def test_rv_positive_for_varying_prices():
    """Prices with genuine variation should produce positive RV."""
    import random
    random.seed(42)
    closes = [100.0]
    for _ in range(25):
        closes.append(closes[-1] * (1 + random.gauss(0, 0.01)))
    rv = compute_realized_vol(closes, window=20)
    assert rv is not None
    assert rv > 0


def test_rv_series_length():
    """Series should have n - window entries."""
    closes = [100.0 + i * 0.1 for i in range(30)]
    series = compute_rv_series(closes, window=20)
    assert len(series) == 30 - 20   # entries from index 20 to 29


def test_rv_series_entries_have_date_and_rv():
    """Each entry has 'date' (str) and 'rv' (float)."""
    closes = [100.0] * 25
    series = compute_rv_series(closes, window=20)
    assert len(series) > 0
    for entry in series:
        assert "date" in entry
        assert "rv" in entry
        assert isinstance(entry["date"], str)
        assert isinstance(entry["rv"], float)
