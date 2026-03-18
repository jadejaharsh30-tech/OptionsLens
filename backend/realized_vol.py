# optionslens/backend/realized_vol.py
"""
Realized volatility from daily closing prices.
Uses close-to-close log-return estimator, annualised by sqrt(252).

RV_N = sqrt(252 / N * sum(ln(S_i / S_{i-1})^2))

Input: plain list of closing prices in chronological order (oldest first).
This matches the output of fyers_client.fetch_historical_prices() directly.
"""
import math
from datetime import date, timedelta
from typing import Optional


def compute_realized_vol(
    closes: list[float],   # chronological order, oldest first
    window: int = 20,
) -> Optional[float]:
    """
    Compute annualised realized vol over the most recent `window` trading days.

    Args:
        closes: list of daily closing prices, chronological (oldest → newest)
        window: number of return periods (requires window+1 prices)

    Returns:
        Annualised RV as decimal (e.g. 0.15 = 15%), or None if insufficient data.
    """
    if len(closes) < window + 1:
        return None

    # Take the most recent window+1 prices
    recent = closes[-(window + 1):]

    log_returns = []
    for i in range(1, len(recent)):
        if recent[i - 1] <= 0 or recent[i] <= 0:
            continue
        log_returns.append(math.log(recent[i] / recent[i - 1]))

    if len(log_returns) < window:
        return None

    # Biased variance estimator (standard for realized vol)
    mean_r   = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_r) ** 2 for r in log_returns) / len(log_returns)

    return math.sqrt(variance * 252)


def compute_rv_series(
    closes: list[float],   # chronological, oldest first
    window: int = 20,
) -> list[dict]:
    """
    Compute rolling RV for every trading day once enough history exists.

    Returns list of {"date": str, "rv": float} in chronological order (oldest first).
    Dates are estimated by counting back from today at 1 calendar day per trading day.
    RV is annualised decimal.

    Note: dates are approximate — they represent trading days, not calendar days.
    The IVvsRVPanel uses these for the x-axis trend, not precise calendar alignment.
    """
    if len(closes) < window + 1:
        return []

    results = []
    n = len(closes)

    # Estimate trading-day dates going backwards from today
    today = date.today()
    # Build a simple mapping: closes[-1] = today, closes[-2] = yesterday, etc.
    # We skip weekends in the reverse mapping
    trading_dates = []
    d = today
    for _ in range(n):
        while d.weekday() >= 5:   # skip Saturday/Sunday
            d -= timedelta(days=1)
        trading_dates.append(d.isoformat())
        d -= timedelta(days=1)
    trading_dates.reverse()   # now chronological, oldest first

    # Compute rolling RV starting from index `window`
    for i in range(window, n):
        slice_ = closes[i - window: i + 1]   # window+1 prices
        rv = compute_realized_vol(slice_, window=window)
        if rv is not None and i < len(trading_dates):
            results.append({"date": trading_dates[i], "rv": rv})

    return results
