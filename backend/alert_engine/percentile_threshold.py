# optionslens/backend/alert_engine/percentile_threshold.py
"""
Adaptive OI spike threshold derived from the Nth percentile of historical
oi_change_pct values stored in the oi_snapshots table.

When the engine has collected enough OI history across recent sessions,
this replaces the static flat threshold with one that is calibrated to each
symbol's own typical distribution — making Stage 1 a genuine outlier detector
rather than an arbitrary absolute cutoff.

Falls back to the static threshold when insufficient history exists
(first day of use, or after clearing the DB).
"""
import math
import sqlite3
from typing import Optional

from alert_engine.models import ALERT_ENGINE_DB


def get_oi_pct_percentile(
    db_path: str = ALERT_ENGINE_DB,
    symbol: str = "NIFTY",
    option_type: str = "CE",
    percentile: float = 90.0,
    min_samples: int = 50,
    lookback_sessions: int = 10,
) -> Optional[float]:
    """
    Compute the Nth percentile of oi_change_pct for a given symbol + option_type
    from the most recent N session dates stored in oi_snapshots.

    Args:
        db_path: path to oi_engine.db
        symbol: e.g. "NIFTY"
        option_type: "CE" or "PE"
        percentile: e.g. 90.0 for 90th percentile
        min_samples: minimum number of rows needed before returning a value
        lookback_sessions: how many past session_dates to include

    Returns:
        Nth percentile value as float, or None if insufficient data.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    c    = conn.cursor()

    # Get the N most recent distinct session dates for this symbol
    c.execute("""
        SELECT DISTINCT session_date FROM oi_snapshots
        WHERE symbol = ?
        ORDER BY session_date DESC
        LIMIT ?
    """, (symbol, lookback_sessions))
    session_dates = [r[0] for r in c.fetchall()]

    if not session_dates:
        conn.close()
        return None

    placeholders = ",".join("?" * len(session_dates))
    c.execute(f"""
        SELECT oi_change_pct FROM oi_snapshots
        WHERE symbol       = ?
          AND option_type  = ?
          AND session_date IN ({placeholders})
          AND oi_change_pct IS NOT NULL
          AND oi_change_pct > 0
        ORDER BY oi_change_pct ASC
    """, [symbol, option_type] + session_dates)

    values = [r[0] for r in c.fetchall()]
    conn.close()

    if len(values) < min_samples:
        return None

    # Linear interpolation percentile
    n   = len(values)
    idx = (percentile / 100.0) * (n - 1)
    lo  = int(math.floor(idx))
    hi  = int(math.ceil(idx))

    if lo == hi:
        return float(values[lo])

    frac = idx - lo
    return float(values[lo] * (1 - frac) + values[hi] * frac)


def get_adaptive_threshold(
    db_path: str = ALERT_ENGINE_DB,
    symbol: str = "NIFTY",
    option_type: str = "CE",
    percentile: float = 90.0,
    min_samples: int = 50,
    static_fallback: float = 500.0,
) -> float:
    """
    Returns the adaptive (percentile-based) threshold when sufficient history
    exists, otherwise returns static_fallback.

    Always returns a positive float — safe to use directly as a threshold.
    """
    adaptive = get_oi_pct_percentile(
        db_path, symbol, option_type, percentile, min_samples
    )
    if adaptive is not None and adaptive > 0:
        return adaptive
    return static_fallback
