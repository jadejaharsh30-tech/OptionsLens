# optionslens/backend/svi_engine.py
"""
SVI (Stochastic Volatility Inspired) surface interpolation.
Gatheral (2004): w(k) = a + b*(rho*(k-m) + sqrt((k-m)^2 + sigma^2))
where k = log(K/S), w = total variance = IV^2 * T.

Fits 5 parameters per expiry slice via gradient descent (pure Python, no scipy).
Returns None for slices with fewer than 4 data points.
"""
import math
from typing import Optional

SVIParams = tuple[float, float, float, float, float]  # a, b, rho, m, sigma


def svi_total_variance(k: float, a: float, b: float, rho: float,
                       m: float, sigma: float) -> float:
    """
    SVI total variance at log-moneyness k.
    w(k) = a + b * (rho*(k-m) + sqrt((k-m)^2 + sigma^2))
    """
    disc = math.sqrt((k - m) ** 2 + sigma ** 2)
    return a + b * (rho * (k - m) + disc)


def svi_iv_at_moneyness(moneyness: float, T: float, params: SVIParams) -> float:
    """
    Convert SVI total variance to annualised IV at a given moneyness K/S.
    moneyness: K / spot  (1.0 = ATM)
    T: years to expiry
    Returns IV as decimal (e.g. 0.15 for 15%).
    """
    k = math.log(moneyness)
    w = svi_total_variance(k, *params)
    w = max(w, 1e-10)  # guard against numerical negatives
    return math.sqrt(w / T)


def fit_svi_slice(
    iv_data: list[tuple[float, float]],  # [(moneyness, iv_decimal), ...]
    T: float,
    learning_rate: float = 1e-4,
    max_iter: int = 5000,
    tol: float = 1e-8,
) -> Optional[SVIParams]:
    """
    Fit SVI parameters to one expiry slice by minimising MSE on total variance.

    Args:
        iv_data: list of (moneyness, iv) tuples — moneyness = K/S, iv = decimal
        T: time to expiry in years
        learning_rate: gradient descent step size
        max_iter: maximum iterations
        tol: convergence tolerance on loss improvement

    Returns:
        (a, b, rho, m, sigma) or None if insufficient data or fit fails
    """
    if len(iv_data) < 4:
        return None

    # Convert to (log-moneyness, total-variance) pairs
    points = [(math.log(m), iv ** 2 * T)
              for m, iv in iv_data if iv is not None and iv > 0]
    if len(points) < 4:
        return None

    # Initialise params from data statistics
    w_vals = [w for _, w in points]
    a     = max(min(w_vals) * 0.8, 1e-5)
    b     = 0.1
    rho   = -0.3
    m     = 0.0
    sigma = 0.1

    prev_loss = float('inf')

    for _ in range(max_iter):
        loss = _mse(points, a, b, rho, m, sigma)
        if abs(prev_loss - loss) < tol:
            break
        prev_loss = loss

        eps = 1e-6
        da     = (_mse(points, a + eps, b, rho, m, sigma)    - loss) / eps
        db     = (_mse(points, a, b + eps, rho, m, sigma)    - loss) / eps
        drho   = (_mse(points, a, b, rho + eps, m, sigma)    - loss) / eps
        dm     = (_mse(points, a, b, rho, m + eps, sigma)    - loss) / eps
        dsigma = (_mse(points, a, b, rho, m, sigma + eps)    - loss) / eps

        a     -= learning_rate * da
        b     -= learning_rate * db
        rho   -= learning_rate * drho
        m     -= learning_rate * dm
        sigma -= learning_rate * dsigma

        # Box constraints — keep params in valid SVI range
        a     = max(a, 1e-6)
        b     = max(b, 1e-6)
        rho   = max(-0.999, min(0.999, rho))
        sigma = max(1e-4, sigma)

        # Enforce minimum variance positivity: a + b*sigma*sqrt(1-rho^2) >= 0
        min_var = a + b * sigma * math.sqrt(1 - rho ** 2)
        if min_var < 0:
            a = max(a, -b * sigma * math.sqrt(1 - rho ** 2) + 1e-6)

    # Final sanity check: fitted IVs must be in plausible range [1%, 200%]
    for k, _ in points:
        w = svi_total_variance(k, a, b, rho, m, sigma)
        if w <= 0 or math.sqrt(w / T) > 2.0 or math.sqrt(w / T) < 0.01:
            return None

    return (a, b, rho, m, sigma)


def _mse(points: list[tuple[float, float]], a: float, b: float,
         rho: float, m: float, sigma: float) -> float:
    """Mean squared error between fitted and market total variances."""
    total = 0.0
    for k, w_market in points:
        w_fit  = svi_total_variance(k, a, b, rho, m, sigma)
        total += (w_fit - w_market) ** 2
    return total / len(points)


def interpolate_surface(
    raw_surface: list[dict],
    spot: float,
    moneyness_grid: list[float] | None = None,
) -> list[dict]:
    """
    Fill gaps in the raw surface with SVI-interpolated points.
    Only adds rows for moneyness values not already present in each expiry.
    Original rows are NOT modified — this only appends new rows.

    Args:
        raw_surface: list of surface row dicts (each has expiry_date, days_to_expiry,
                     moneyness, mid_iv, etc.)
        spot: current spot price (used to compute approximate strike)
        moneyness_grid: target moneyness values to evaluate at.
                        Defaults to 21 points from 0.85 to 1.15 (1.5% steps).

    Returns:
        List of new interpolated rows (does NOT include the original raw rows).
    """
    if moneyness_grid is None:
        moneyness_grid = [round(0.85 + i * 0.015, 3) for i in range(21)]

    # Group by expiry
    by_expiry: dict[str, list[dict]] = {}
    for row in raw_surface:
        by_expiry.setdefault(row["expiry_date"], []).append(row)

    interpolated_rows = []

    for expiry_date, rows in by_expiry.items():
        T = rows[0]["days_to_expiry"] / 365.0
        if T <= 0:
            continue

        # Build iv_data using mid_iv (best per-strike estimate)
        iv_data = [
            (r["moneyness"], r["mid_iv"] / 100.0)
            for r in rows
            if r["mid_iv"] is not None
        ]

        params = fit_svi_slice(iv_data, T)
        if params is None:
            continue  # fewer than 4 points or fit failed — skip

        existing_moneyness = {r["moneyness"] for r in rows}

        for m in moneyness_grid:
            # Skip if an existing point is already within 0.5% of this target
            if any(abs(em - m) < 0.005 for em in existing_moneyness):
                continue

            try:
                iv_pct = round(svi_iv_at_moneyness(m, T, params) * 100, 2)
            except (ValueError, ZeroDivisionError):
                continue

            if not (1.0 <= iv_pct <= 200.0):
                continue

            interpolated_rows.append({
                "expiry_date":    expiry_date,
                "days_to_expiry": rows[0]["days_to_expiry"],
                "strike":         round(spot * m, 0),
                "moneyness":      m,
                "call_iv":        None,  # no call/put split for interpolated points
                "put_iv":         None,
                "mid_iv":         iv_pct,
                "interpolated":   True,
            })

    return interpolated_rows
