# optionslens/backend/bhavcopy/spot.py
"""
The underlying's closing price for dates whose exchange file does not publish it.

NSE's derivatives files carry the underlying price only from 2024-07-08 (the
UDiFF format). Before that there is none, and every legacy date was being
skipped — which is most of the available history.

THE ESTIMATE
    Under cost of carry, F = S·e^((r − q)T). Backing S out of the front-month
    future's close with the configured risk-free rate and NO dividend yield:

        S ≈ F · e^(−r·T)

    ignores q, so the estimate sits high by a factor e^(q·T). For NIFTY
    (q about 1.2-1.5%) and a front contract at most ~35 days out, that is at
    most about 0.15%, and it moves slowly and smoothly with T.

WHAT THAT ERROR DOES AND DOES NOT AFFECT
    - Implied vol: nothing. Black-76 prices off the forward recovered from the
      chain's own put-call parity; spot is only a sanity reference that
      rejects a wildly wrong forward.
    - Realised vol (vrp's RV leg): almost nothing. A level bias that drifts by
      about 0.004% a day cancels out of daily returns. The one visible effect
      is at each monthly roll, when T jumps from near zero to ~30 days and the
      bias shifts by up to ~0.1% in one day — against a typical daily move of
      about 1%, a negligible addition to realised variance.
    - Basis: everything. Basis measured against a spot derived from the future
      is circular, so `futures.load_basis_history` never uses these rows.

Every estimated close is stored with source="futures_estimate", never mixed
silently with published closes.

Shared by the importer (spot_history) and the EOD snapshot adapter
(ChainSnapshot.spot) so the two cannot estimate differently.
"""
import math
import sqlite3
from datetime import datetime
from typing import Optional

from market_hours import EXPIRY_TIME_IST, IST, time_to_expiry

SOURCE_PUBLISHED = "published"
SOURCE_ESTIMATE = "futures_estimate"


def front_future(conn: sqlite3.Connection, symbol: str, trad_dt: str,
                 ) -> Optional[tuple[float, str]]:
    """
    (close, expiry_dt ISO) of the nearest traded future not expiring today.

    Same rules as the options: `expiry_dt > trad_dt` because on expiry day the
    settlement column carries the index level, and volume > 0 because an
    untraded contract republishes yesterday's close.
    """
    try:
        row = conn.execute("""
            SELECT close, expiry_dt FROM daily_future
            WHERE symbol = ? AND trad_dt = ? AND expiry_dt > trad_dt
              AND close > 0 AND volume > 0
            ORDER BY expiry_dt LIMIT 1
        """, (symbol.upper(), trad_dt)).fetchone()
    except sqlite3.Error:
        return None
    return (float(row[0]), row[1]) if row else None


def estimate_spot(futures_close: float, expiry_dt: str, trad_dt: str,
                  rate: float) -> Optional[float]:
    """S ≈ F·e^(−r·T), T from the trading date's 15:30 close to expiry."""
    if not futures_close or futures_close <= 0:
        return None
    close = datetime.combine(datetime.strptime(trad_dt, "%Y-%m-%d").date(),
                             EXPIRY_TIME_IST, tzinfo=IST)
    fyers_expiry = datetime.strptime(expiry_dt, "%Y-%m-%d").strftime("%d-%m-%Y")
    T = time_to_expiry(fyers_expiry, now=close)
    if T <= 0:
        return None
    return futures_close * math.exp(-rate * T)


def spot_for_date(conn: sqlite3.Connection, symbol: str, trad_dt: str,
                  published: Optional[float], rate: Optional[float] = None,
                  ) -> tuple[Optional[float], Optional[str]]:
    """
    (spot, source): the published price when there is one, else the estimate
    from the front future, else (None, None). A published price always wins.
    """
    if published and published > 0:
        return float(published), SOURCE_PUBLISHED
    fut = front_future(conn, symbol, trad_dt)
    if fut is None:
        return None, None
    if rate is None:
        from config import RISK_FREE_RATE
        rate = RISK_FREE_RATE
    est = estimate_spot(fut[0], fut[1], trad_dt, rate)
    return (est, SOURCE_ESTIMATE) if est else (None, None)
