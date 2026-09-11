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


# ── Black-76: options priced off the FORWARD, not spot ────────────────────────
#
# Indian index options are European and the market prices them off the forward
# (equivalently, the futures price), not off spot. Discounting spot at the
# risk-free rate with no dividend yield — which `bs_price` above does — puts the
# model forward above the true forward by roughly the dividend yield (~1.2-1.5%
# on NIFTY). That error does not cancel between calls and puts: it inflates
# call IVs and deflates put IVs at the same strike, so put-call parity fails in
# our own solved IVs and part of the "skew" on the surface chart is model error
# rather than market skew.
#
# Black-76 removes the problem by taking the forward as an observable:
#     call = e^(-rT) [F N(d1) - K N(d2)]
#     put  = e^(-rT) [K N(-d2) - F N(-d1)]
#     d1   = (ln(F/K) + sigma^2 T / 2) / (sigma sqrt(T)),  d2 = d1 - sigma sqrt(T)
#
# Setting F = S e^(rT) reproduces the spot-based Black-Scholes results exactly,
# so this is a strict generalisation.


def _d1_d2_forward(F: float, K: float, T: float, sigma: float) -> tuple[float, float]:
    """d1/d2 in forward space. No rate term — it lives in the discount factor."""
    d1 = (math.log(F / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def black76_price(F: float, K: float, T: float, r: float,
                  sigma: float, option_type: OptionType) -> float:
    """
    Black-76 price of a European option on a forward/future.

    Args:
        F: Forward price of the underlying for this expiry
        K: Strike
        T: Time to expiry in years
        r: Risk-free rate, used only for discounting
        sigma: Volatility as a decimal
        option_type: "CE" or "PE"
    """
    if T <= 0:
        return max(F - K, 0.0) if option_type == "CE" else max(K - F, 0.0)

    d1, d2 = _d1_d2_forward(F, K, T, sigma)
    disc = math.exp(-r * T)

    if option_type == "CE":
        return disc * (F * _norm_cdf(d1) - K * _norm_cdf(d2))
    return disc * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1))


def black76_vega(F: float, K: float, T: float, r: float, sigma: float) -> float:
    """Vega in forward space (per unit vol). Identical for calls and puts."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1, _ = _d1_d2_forward(F, K, T, sigma)
    return math.exp(-r * T) * F * _norm_pdf(d1) * math.sqrt(T)


def implied_vol_forward(
    market_price: float,
    F: float,
    K: float,
    T: float,
    r: float,
    option_type: OptionType,
    max_iter: int = 100,
    tol: float = 1e-6,
    initial_guess: float = 0.3,
) -> Optional[float]:
    """
    Implied volatility against Black-76. Newton-Raphson, same as the spot solver.

    Because calls and puts at one strike share a single forward, the call IV and
    put IV they produce agree to within bid-ask noise — which is the whole point
    of doing this. Returns None on the same conditions as `implied_volatility`.
    """
    if market_price is None or market_price <= 0 or T <= 0 or F <= 0:
        return None

    # Below intrinsic there is no solution; this is a stale or crossed quote.
    intrinsic = (max(F - K, 0.0) if option_type == "CE" else max(K - F, 0.0)) \
        * math.exp(-r * T)
    if market_price < intrinsic - 1e-9:
        return None

    sigma = initial_guess
    for _ in range(max_iter):
        price = black76_price(F, K, T, r, sigma, option_type)
        vega  = black76_vega(F, K, T, r, sigma)

        if abs(vega) < 1e-10:
            return None

        diff = price - market_price
        if abs(diff) < tol:
            return sigma if 0 < sigma < 10 else None

        sigma = sigma - diff / vega
        sigma = max(1e-6, min(sigma, 10.0))

    return None


def black76_greeks(
    F: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: OptionType,
    spot: Optional[float] = None,
) -> dict:
    """
    Greeks under Black-76.

    Delta and gamma are returned with respect to SPOT when `spot` is supplied,
    since that is what a delta-hedger trades and what the GEX model assumes.
    The conversion needs no separate dividend-yield estimate: with
    F = S e^((r-q)T), dF/dS is exactly F/S, which we already observe.

    Theta is per calendar day; vega and rho are per 1% move, matching the
    existing spot-based `greeks()` so the two are interchangeable downstream.
    """
    if T <= 0 or sigma <= 0 or F <= 0:
        return {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0}

    d1, d2 = _d1_d2_forward(F, K, T, sigma)
    nd1  = _norm_pdf(d1)
    disc = math.exp(-r * T)

    # Forward-space delta/gamma, then chain-rule into spot space.
    if option_type == "CE":
        delta_fwd = disc * _norm_cdf(d1)
    else:
        delta_fwd = -disc * _norm_cdf(-d1)
    gamma_fwd = disc * nd1 / (F * sigma * math.sqrt(T))

    dF_dS = (F / spot) if (spot and spot > 0) else 1.0
    delta = delta_fwd * dF_dS
    gamma = gamma_fwd * dF_dS ** 2

    vega = disc * F * nd1 * math.sqrt(T) / 100.0

    # theta = r*price - e^(-rT) F phi(d1) sigma / (2 sqrt(T)), per year.
    price = black76_price(F, K, T, r, sigma, option_type)
    theta = (r * price - disc * F * nd1 * sigma / (2 * math.sqrt(T))) / 365.0

    # With F taken as an observable, the only rate sensitivity is discounting.
    rho = -T * price / 100.0

    # Delta and gamma are kept at far finer precision than the other Greeks.
    # Index gamma is ~1e-4 and GEX multiplies it by spot^2 (~6e8 for NIFTY), so
    # rounding gamma at 1e-6 injects visible error into the GEX profile — and
    # far-OTM strikes, where gamma is below 1e-6, would round away to exactly
    # zero and silently drop out of the gamma surface altogether.
    return {
        "delta": round(delta, 10),
        "gamma": round(gamma, 12),
        "vega":  round(vega,  6),
        "theta": round(theta, 6),
        "rho":   round(rho,   6),
    }


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
