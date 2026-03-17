# optionslens/backend/iv_engine.py
"""
Black-Scholes IV engine.
Pure Python — no external quant libraries.
Implements: BS pricer, Vega, Newton-Raphson IV solver, all Greeks.
"""
import math
from typing import Literal, Optional

OptionType = Literal["CE", "PE"]


def _norm_cdf(x: float) -> float:
    """Standard normal CDF using math.erf."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    """Compute d1 and d2 for Black-Scholes."""
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def bs_price(S: float, K: float, T: float, r: float,
             sigma: float, option_type: OptionType) -> float:
    """
    Black-Scholes option price.

    Args:
        S: Current spot price
        K: Strike price
        T: Time to expiry in years (e.g. 30/365)
        r: Risk-free rate (e.g. 0.065)
        sigma: Volatility (e.g. 0.25 for 25%)
        option_type: "CE" for call, "PE" for put

    Returns:
        Option price
    """
    if T <= 0:
        # At expiry: intrinsic value only
        if option_type == "CE":
            return max(S - K, 0.0)
        else:
            return max(K - S, 0.0)

    d1, d2 = _d1_d2(S, K, T, r, sigma)

    if option_type == "CE":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """
    Vega: sensitivity of option price to a 1-unit change in volatility.
    Identical for calls and puts.

    Returns:
        Vega (divide by 100 to get price change per 1% IV move)
    """
    if T <= 0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return S * _norm_pdf(d1) * math.sqrt(T)


def implied_volatility(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: OptionType,
    max_iter: int = 100,
    tol: float = 1e-6,
    initial_guess: float = 0.3,
) -> Optional[float]:
    """
    Implied volatility via Newton-Raphson iteration.

    Solves: BS(sigma) = market_price
    Update: sigma_new = sigma_old - (BS(sigma_old) - market_price) / Vega(sigma_old)

    Returns:
        Implied volatility as a decimal (e.g. 0.25 = 25%), or None if:
        - market_price <= 0 (illiquid / missing)
        - solver fails to converge
        - vega is near zero (deep OTM, can't solve)
    """
    if market_price is None or market_price <= 0:
        return None
    if T <= 0:
        return None

    sigma = initial_guess

    for _ in range(max_iter):
        price = bs_price(S, K, T, r, sigma, option_type)
        vega  = bs_vega(S, K, T, r, sigma)

        if abs(vega) < 1e-10:
            # Vega too small to divide — deep OTM, can't solve
            return None

        diff = price - market_price
        if abs(diff) < tol:
            return sigma if 0 < sigma < 10 else None  # sanity bound

        sigma = sigma - diff / vega

        # Clamp sigma to valid range each iteration
        sigma = max(1e-6, min(sigma, 10.0))

    return None  # Did not converge


def greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: OptionType,
) -> dict:
    """
    Compute all first-order Greeks + Gamma.

    Returns dict with keys: delta, gamma, vega, theta, rho
    Theta is per calendar day (divided by 365).
    Vega is per 1% move in IV (divided by 100).
    """
    if T <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0}

    d1, d2 = _d1_d2(S, K, T, r, sigma)
    nd1    = _norm_pdf(d1)
    disc   = math.exp(-r * T)

    # Gamma (same for calls and puts)
    gamma = nd1 / (S * sigma * math.sqrt(T))

    # Vega (same for calls and puts), per 1% IV move
    vega = S * nd1 * math.sqrt(T) / 100.0

    if option_type == "CE":
        delta = _norm_cdf(d1)
        theta = (
            -(S * nd1 * sigma) / (2 * math.sqrt(T))
            - r * K * disc * _norm_cdf(d2)
        ) / 365.0
        rho = K * T * disc * _norm_cdf(d2) / 100.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (
            -(S * nd1 * sigma) / (2 * math.sqrt(T))
            + r * K * disc * _norm_cdf(-d2)
        ) / 365.0
        rho = -K * T * disc * _norm_cdf(-d2) / 100.0

    return {
        "delta": round(delta, 6),
        "gamma": round(gamma, 6),
        "vega":  round(vega,  6),
        "theta": round(theta, 6),
        "rho":   round(rho,   6),
    }
