"""
Forward-based pricing tests.

The headline property: when calls and puts at one strike are priced off a shared
forward, their solved IVs agree. Under the old spot-based model they diverged
systematically by roughly the dividend yield, and that artefact was being read
off the surface chart as skew.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chain_pricing import implied_forward_for_chain, parity_pairs, price_for_iv  # noqa: E402
from forward_engine import (  # noqa: E402
    forward_basis_pct, forward_from_strike, implied_dividend_yield,
    implied_forward, implied_forward_robust,
)
from iv_engine import (  # noqa: E402
    black76_greeks, black76_price, black76_vega, bs_price,
    implied_vol_forward,
)

R = 0.065
T = 30 / 365


# ── Black-76 consistency ──────────────────────────────────────────────────────

def test_black76_matches_black_scholes_when_forward_is_spot_compounded():
    """F = S e^(rT) with zero dividend must reproduce the spot model exactly."""
    S, K, sigma = 24500.0, 24500.0, 0.15
    F = S * math.exp(R * T)

    for opt in ("CE", "PE"):
        assert abs(
            black76_price(F, K, T, R, sigma, opt) - bs_price(S, K, T, R, sigma, opt)
        ) < 1e-8


def test_black76_satisfies_put_call_parity():
    """C - P = e^(-rT)(F - K), to machine precision."""
    F, K, sigma = 24600.0, 24500.0, 0.15
    call = black76_price(F, K, T, R, sigma, "CE")
    put  = black76_price(F, K, T, R, sigma, "PE")
    assert abs((call - put) - math.exp(-R * T) * (F - K)) < 1e-9


def test_black76_vega_is_identical_for_calls_and_puts():
    F, K, sigma = 24500.0, 24700.0, 0.18
    v = black76_vega(F, K, T, R, sigma)
    assert v > 0
    # Bump-and-reprice agrees with the closed form.
    bump = 1e-6
    for opt in ("CE", "PE"):
        numeric = (black76_price(F, K, T, R, sigma + bump, opt)
                   - black76_price(F, K, T, R, sigma, opt)) / bump
        assert abs(numeric - v) < 1e-3


def test_intrinsic_at_expiry():
    assert black76_price(24600, 24500, 0, R, 0.15, "CE") == 100.0
    assert black76_price(24400, 24500, 0, R, 0.15, "PE") == 100.0
    assert black76_price(24400, 24500, 0, R, 0.15, "CE") == 0.0


# ── IV solver ─────────────────────────────────────────────────────────────────

def test_iv_roundtrip_recovers_the_input_vol():
    F, K, sigma = 24600.0, 24500.0, 0.1734
    for opt in ("CE", "PE"):
        price = black76_price(F, K, T, R, sigma, opt)
        assert abs(implied_vol_forward(price, F, K, T, R, opt) - sigma) < 1e-6


def test_call_and_put_iv_agree_at_the_same_strike():
    """
    The reason this whole item exists.

    Build consistent market prices from one forward and one vol, then solve each
    leg independently. Both must return the same IV.
    """
    F, K, sigma = 24612.5, 24500.0, 0.1612
    call_iv = implied_vol_forward(black76_price(F, K, T, R, sigma, "CE"),
                                  F, K, T, R, "CE")
    put_iv  = implied_vol_forward(black76_price(F, K, T, R, sigma, "PE"),
                                  F, K, T, R, "PE")
    assert abs(call_iv - put_iv) < 1e-6


def test_solver_rejects_unsolvable_inputs():
    F, K = 24500.0, 24500.0
    assert implied_vol_forward(0, F, K, T, R, "CE") is None
    assert implied_vol_forward(100, F, K, 0, R, "CE") is None
    # Below intrinsic — a crossed or stale quote, not a low-vol option.
    deep_itm_intrinsic = math.exp(-R * T) * (25000 - 24000)
    assert implied_vol_forward(deep_itm_intrinsic * 0.5, 25000, 24000,
                               T, R, "CE") is None


# ── Forward extraction ────────────────────────────────────────────────────────

def test_forward_recovered_from_parity_at_a_single_strike():
    F_true, sigma = 24580.0, 0.15
    K = 24500.0
    call = black76_price(F_true, K, T, R, sigma, "CE")
    put  = black76_price(F_true, K, T, R, sigma, "PE")
    assert abs(forward_from_strike(K, call, put, T, R) - F_true) < 1e-6


def test_implied_forward_picks_min_abs_call_minus_put():
    """VIX-style rule: the strike nearest the forward is the most informative."""
    F_true, sigma = 24580.0, 0.15
    pairs = [
        (k,
         black76_price(F_true, k, T, R, sigma, "CE"),
         black76_price(F_true, k, T, R, sigma, "PE"))
        for k in (24000, 24500, 24600, 25000)
    ]
    assert abs(implied_forward(pairs, T, R, spot=24500.0) - F_true) < 1e-6


def test_implied_forward_is_strike_independent_on_clean_data():
    F_true, sigma = 24580.0, 0.15
    for k in (23500, 24500, 25500):
        call = black76_price(F_true, k, T, R, sigma, "CE")
        put  = black76_price(F_true, k, T, R, sigma, "PE")
        assert abs(forward_from_strike(k, call, put, T, R) - F_true) < 1e-6


def test_robust_forward_uses_median_of_near_atm_strikes():
    F_true, sigma = 24580.0, 0.15
    pairs = [
        (k,
         black76_price(F_true, k, T, R, sigma, "CE"),
         black76_price(F_true, k, T, R, sigma, "PE"))
        for k in (24300, 24400, 24500, 24600, 24700)
    ]
    got = implied_forward_robust(pairs, T, R, spot=24500.0)
    assert abs(got - F_true) < 1e-6


def test_absurd_forward_is_rejected_not_propagated():
    """A crossed quote must not poison every IV on the chain."""
    pairs = [(24500.0, 9000.0, 1.0)]  # implies F ~= 33.5k against 24.5k spot
    assert implied_forward(pairs, T, R, spot=24500.0) is None


def test_forward_needs_two_usable_quotes():
    assert implied_forward([(24500.0, 100.0, None)], T, R, spot=24500.0) is None
    assert implied_forward([(24500.0, 0.0, 50.0)], T, R, spot=24500.0) is None
    assert implied_forward([], T, R, spot=24500.0) is None
    assert implied_forward([(24500.0, 100.0, 90.0)], 0.0, R) is None


# ── Diagnostics ───────────────────────────────────────────────────────────────

def test_implied_dividend_yield_inverts_the_forward_relation():
    S, q = 24500.0, 0.013
    F = S * math.exp((R - q) * T)
    assert abs(implied_dividend_yield(F, S, T, R) - q) < 1e-9


def test_basis_sign_distinguishes_contango_from_backwardation():
    assert forward_basis_pct(24600.0, 24500.0) > 0
    assert forward_basis_pct(24400.0, 24500.0) < 0


# ── Greeks ────────────────────────────────────────────────────────────────────

def test_spot_delta_conversion_uses_observed_forward_over_spot():
    """
    Delta is reported against spot, since that is what a hedger trades and what
    the GEX model assumes. The conversion factor dF/dS is F/S — no separate
    dividend-yield estimate needed.
    """
    S, K, sigma = 24500.0, 24500.0, 0.15
    F = S * math.exp((R - 0.012) * T)

    fwd_only = black76_greeks(F, K, T, R, sigma, "CE")
    with_spot = black76_greeks(F, K, T, R, sigma, "CE", spot=S)
    assert abs(with_spot["delta"] - fwd_only["delta"] * (F / S)) < 1e-6
    assert abs(with_spot["gamma"] - fwd_only["gamma"] * (F / S) ** 2) < 1e-9


def test_greek_signs_and_magnitudes_are_sane():
    F, K, sigma = 24500.0, 24500.0, 0.15
    call = black76_greeks(F, K, T, R, sigma, "CE", spot=24500.0)
    put  = black76_greeks(F, K, T, R, sigma, "PE", spot=24500.0)

    assert 0 < call["delta"] < 1
    assert -1 < put["delta"] < 0
    assert call["gamma"] > 0 and put["gamma"] > 0
    assert call["vega"] > 0 and put["vega"] > 0
    assert call["theta"] < 0 and put["theta"] < 0     # long options decay
    assert call["rho"] < 0 and put["rho"] < 0         # discounting only


def test_greeks_are_zero_past_expiry():
    g = black76_greeks(24500.0, 24500.0, 0.0, R, 0.15, "CE")
    assert all(v == 0.0 for v in g.values())


# ── chain_pricing glue ────────────────────────────────────────────────────────

def test_price_for_iv_prefers_mid_over_stale_ltp():
    row = {"strike": 24500, "option_type": "CE", "ltp": 999.0, "bid": 100.0, "ask": 102.0}
    assert price_for_iv(row) == 101.0


def test_price_for_iv_falls_back_to_ltp_on_one_sided_book():
    assert price_for_iv({"ltp": 55.0, "bid": 0, "ask": 60.0}) == 55.0
    assert price_for_iv({"ltp": 0, "bid": 0, "ask": 0}) is None


def test_parity_pairs_collapses_a_flat_chain():
    chain = [
        {"strike": 24500, "option_type": "CE", "ltp": 120.0, "bid": 119.0, "ask": 121.0},
        {"strike": 24500, "option_type": "PE", "ltp": 95.0,  "bid": 94.0,  "ask": 96.0},
        {"strike": 24600, "option_type": "CE", "ltp": 70.0,  "bid": 69.0,  "ask": 71.0},
    ]
    pairs = dict((k, (c, p)) for k, c, p in parity_pairs(chain))
    assert pairs[24500] == (120.0, 95.0)
    assert pairs[24600] == (70.0, None)


def test_forward_from_a_realistic_chain():
    F_true, sigma = 24580.0, 0.15
    chain = []
    for k in (24400, 24500, 24600, 24700):
        for opt in ("CE", "PE"):
            px = black76_price(F_true, k, T, R, sigma, opt)
            chain.append({"strike": k, "option_type": opt,
                          "ltp": px, "bid": px - 0.5, "ask": px + 0.5})
    assert abs(implied_forward_for_chain(chain, T, spot=24500.0) - F_true) < 1.0
