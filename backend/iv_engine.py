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


# ── Shared solver core ────────────────────────────────────────────────────────
#
# Plain Newton-Raphson is fast near the money and unreliable in the wings: vega
# collapses toward zero for far OTM strikes, so the update step either explodes
# or the vega guard bails out and returns None. That is why the edges of the IV
# surface were empty — the data was there, the solver just gave up.
#
# Option price is strictly increasing in volatility, so a bracketed bisection
# always converges when a solution exists. We keep Newton for speed and fall
# back to bisection whenever it misbehaves, which makes the solver total: it
# returns a value for every price inside the no-arbitrage bounds, and None only
# when the quote genuinely admits no solution.

IV_LOWER_BOUND = 1e-4    # 0.01% vol
IV_UPPER_BOUND = 5.0     # 500% vol — anything above is a bad quote, not a vol
BISECTION_MAX_ITER = 200

# Identifiability gate.
#
# Being able to converge is not the same as the answer meaning anything. Deep
# ITM and far OTM options have vega near zero: their price barely moves with
# volatility, so many different vols reprice to the same quote and the solver
# will happily return whichever one it landed on. Silently reporting that as an
# IV is worse than reporting nothing, because it looks like data.
#
# The quote itself sets the resolution limit. NSE option prices move in 0.05
# ticks, so if a one-vol-point change (0.01) moves the model price by less than
# half a tick, the market price simply does not carry that information.
OPTION_TICK_SIZE = 0.05
MIN_VEGA = (OPTION_TICK_SIZE / 2) / 0.01   # = 2.5 price units per unit vol


def _initial_vol_guess(market_price: float, forward: float, T: float) -> float:
    """
    Brenner-Subrahmanyam ATM approximation: sigma ~= sqrt(2*pi/T) * price / F.

    A far better starting point than a flat 0.3 for short-dated options, where
    a bad guess is what pushes Newton out of the bracket in the first place.
    """
    if forward <= 0 or T <= 0:
        return 0.3
    guess = math.sqrt(2.0 * math.pi / T) * market_price / forward
    return min(max(guess, 0.01), 2.0)


def _solve_implied_vol(price_at, vega_at, market_price: float,
                       initial_guess: float, max_iter: int,
                       tol: float, min_vega: float = MIN_VEGA) -> Optional[float]:
    """
    Solve price_at(sigma) == market_price for sigma.

    Args:
        price_at: sigma -> model price (must be increasing in sigma)
        vega_at:  sigma -> d(price)/d(sigma)
        min_vega: reject solutions where the price is too insensitive to vol
                  for the answer to be identifiable (see MIN_VEGA)
    """
    def _accept(sigma: Optional[float]) -> Optional[float]:
        if sigma is None:
            return None
        return sigma if abs(vega_at(sigma)) >= min_vega else None

    lo, hi = IV_LOWER_BOUND, IV_UPPER_BOUND
    price_lo, price_hi = price_at(lo), price_at(hi)

    # Outside the attainable range there is no implied vol to find. This is a
    # crossed/stale quote or an arbitrage violation, not a solver failure.
    if market_price < price_lo - tol or market_price > price_hi + tol:
        return None

    # ── Newton-Raphson ────────────────────────────────────────────────────────
    sigma = min(max(initial_guess, lo), hi)
    for _ in range(max_iter):
        price = price_at(sigma)
        diff  = price - market_price
        if abs(diff) < tol:
            return _accept(sigma)

        vega = vega_at(sigma)
        if abs(vega) < 1e-10:
            break                      # flat in sigma — hand over to bisection

        nxt = sigma - diff / vega
        if not math.isfinite(nxt) or not (lo <= nxt <= hi):
            break                      # left the bracket — hand over to bisection
        sigma = nxt

    # ── Bisection fallback ────────────────────────────────────────────────────
    for _ in range(BISECTION_MAX_ITER):
        mid = 0.5 * (lo + hi)
        price = price_at(mid)
        if abs(price - market_price) < tol:
            return _accept(mid)
        if price < market_price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12:
            break

    mid = 0.5 * (lo + hi)
    return _accept(mid) if abs(price_at(mid) - market_price) < tol * 10 else None


def implied_volatility(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: OptionType,
    max_iter: int = 100,
    tol: float = 1e-6,
    initial_guess: Optional[float] = None,
) -> Optional[float]:
    """
    Implied volatility against spot-based Black-Scholes.

    Prefer `implied_vol_forward` in new code: pricing off spot with no dividend
    yield biases call and put IVs in opposite directions. This is kept for the
    Position Lab and for tests.

    Returns IV as a decimal (0.25 = 25%), or None when the quote admits no
    solution (non-positive price, expired, or outside no-arbitrage bounds).
    """
    if market_price is None or market_price <= 0 or T <= 0:
        return None

    forward = S * math.exp(r * T)
    guess = initial_guess if initial_guess is not None else \
        _initial_vol_guess(market_price, forward, T)

    return _solve_implied_vol(
        price_at = lambda sig: bs_price(S, K, T, r, sig, option_type),
        vega_at  = lambda sig: bs_vega(S, K, T, r, sig),
        market_price  = market_price,
        initial_guess = guess,
        max_iter      = max_iter,
        tol           = tol,
    )


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
    initial_guess: Optional[float] = None,
) -> Optional[float]:
    """
    Implied volatility against Black-76 — the solver the app actually uses.

    Because calls and puts at one strike share a single forward, the call IV and
    put IV they produce agree to within bid-ask noise, which is the whole point
    of pricing off the forward.

    Uses the shared Newton-with-bisection-fallback core, so far OTM strikes
    (where vega collapses and plain Newton bails out) still return a value
    instead of silently vanishing from the surface.
    """
    if market_price is None or market_price <= 0 or T <= 0 or F <= 0:
        return None

    # Below intrinsic there is no solution; this is a stale or crossed quote.
    intrinsic = (max(F - K, 0.0) if option_type == "CE" else max(K - F, 0.0)) \
        * math.exp(-r * T)
    if market_price < intrinsic - 1e-9:
        return None

    guess = initial_guess if initial_guess is not None else \
        _initial_vol_guess(market_price, F, T)

    return _solve_implied_vol(
        price_at = lambda sig: black76_price(F, K, T, r, sig, option_type),
        vega_at  = lambda sig: black76_vega(F, K, T, r, sig),
        market_price  = market_price,
        initial_guess = guess,
        max_iter      = max_iter,
        tol           = tol,
    )


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
