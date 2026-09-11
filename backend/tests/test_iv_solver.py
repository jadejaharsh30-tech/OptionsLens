"""
IV solver robustness tests.

Two properties matter, and the second is the subtle one:

1. The solver should find a vol whenever the quote carries vol information —
   plain Newton-Raphson bails out in the wings where vega collapses, which is
   why the edges of the surface were empty.
2. It must NOT return a number when the quote carries no vol information. Deep
   ITM and far OTM prices are flat in volatility, so a solver that always
   converges will return a confident, wrong answer. A fabricated IV is worse
   than a missing one because it looks like data.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from iv_engine import (  # noqa: E402
    MIN_VEGA, black76_price, black76_vega, bs_price, implied_volatility,
    implied_vol_forward,
)

R = 0.065
F = 24600.0


# ── Recovery across the identifiable range ────────────────────────────────────

def test_roundtrip_across_a_wide_vol_range():
    """From 5% to 300% vol, ATM, where vega is always large."""
    K, T = F, 30 / 365
    for true_vol in (0.05, 0.10, 0.16, 0.35, 0.80, 1.50, 3.00):
        price = black76_price(F, K, T, R, true_vol, "CE")
        got = implied_vol_forward(price, F, K, T, R, "CE")
        assert got is not None, f"failed to solve at vol={true_vol}"
        assert abs(got - true_vol) < 1e-5


def test_roundtrip_across_strikes_that_carry_vol_information():
    T, true_vol = 7 / 365, 0.16
    solved = 0
    for K in (23000, 24000, 24600, 25200, 26000):
        price = black76_price(F, K, T, R, true_vol, "CE")
        got = implied_vol_forward(price, F, K, T, R, "CE")
        assert got is not None and abs(got - true_vol) < 1e-4
        solved += 1
    assert solved == 5


def test_roundtrip_at_very_short_expiry():
    """0DTE: hours to expiry, where a flat 0.3 initial guess used to diverge."""
    T, true_vol = 3 / (365 * 24), 0.22
    price = black76_price(F, F, T, R, true_vol, "CE")
    got = implied_vol_forward(price, F, F, T, R, "CE")
    assert got is not None and abs(got - true_vol) < 1e-5


def test_answer_is_independent_of_the_initial_guess():
    """If the guess changes the answer, the solver is not actually converging."""
    K, T, true_vol = 25000.0, 14 / 365, 0.19
    price = black76_price(F, K, T, R, true_vol, "CE")
    results = [
        implied_vol_forward(price, F, K, T, R, "CE", initial_guess=g)
        for g in (0.01, 0.3, 1.0, 4.0)
    ]
    assert all(r is not None for r in results)
    assert max(results) - min(results) < 1e-5


def test_bisection_rescues_a_newton_hostile_start():
    """A guess pinned at the far end of the bracket must still land correctly."""
    K, T, true_vol = 25500.0, 5 / 365, 0.21
    price = black76_price(F, K, T, R, true_vol, "CE")
    got = implied_vol_forward(price, F, K, T, R, "CE", initial_guess=4.99)
    assert got is not None and abs(got - true_vol) < 1e-4


# ── Identifiability gate: silence beats a confident wrong answer ──────────────

def test_deep_itm_is_rejected_rather_than_guessed():
    """
    Price is ~all intrinsic and vega ~1e-16. Before the gate the solver returned
    26.25% for a true 16% vol — converged, confident, and wrong.
    """
    T, true_vol = 7 / 365, 0.16
    for K in (20000, 22000):
        price = black76_price(F, K, T, R, true_vol, "CE")
        assert black76_vega(F, K, T, R, true_vol) < MIN_VEGA
        assert implied_vol_forward(price, F, K, T, R, "CE") is None


def test_far_otm_worthless_options_are_rejected():
    T, true_vol = 7 / 365, 0.16
    for K in (27000, 30000):
        price = black76_price(F, K, T, R, true_vol, "CE")
        assert implied_vol_forward(price, F, K, T, R, "CE") is None


def test_gate_never_returns_a_materially_wrong_vol():
    """
    Sweep strikes and expiries: every non-None answer must be correct.
    This is the property that makes the surface trustworthy.
    """
    true_vol = 0.18
    for T in (1 / 365, 7 / 365, 30 / 365, 90 / 365):
        for K in range(19000, 31000, 250):
            for opt in ("CE", "PE"):
                price = black76_price(F, float(K), T, R, true_vol, opt)
                got = implied_vol_forward(price, F, float(K), T, R, opt)
                if got is not None:
                    assert abs(got - true_vol) < 1e-3, (
                        f"wrong IV {got:.4f} at K={K} T={T:.4f} {opt}"
                    )


def test_near_the_money_is_always_identifiable():
    """The gate must not throw away the strikes that actually matter."""
    true_vol = 0.18
    for T in (1 / 365, 7 / 365, 30 / 365):
        for K in (F * 0.98, F, F * 1.02):
            price = black76_price(F, K, T, R, true_vol, "CE")
            assert implied_vol_forward(price, F, K, T, R, "CE") is not None


# ── Rejection of genuinely unsolvable input ───────────────────────────────────

def test_non_positive_and_expired_inputs():
    T = 30 / 365
    assert implied_vol_forward(0, F, F, T, R, "CE") is None
    assert implied_vol_forward(-5, F, F, T, R, "CE") is None
    assert implied_vol_forward(None, F, F, T, R, "CE") is None
    assert implied_vol_forward(100, F, F, 0, R, "CE") is None
    assert implied_vol_forward(100, 0, F, T, R, "CE") is None


def test_price_above_no_arbitrage_bound_is_rejected():
    """A call cannot be worth more than the discounted forward."""
    T = 30 / 365
    absurd = F * math.exp(-R * T) * 1.5
    assert implied_vol_forward(absurd, F, F, T, R, "CE") is None


# ── Spot-based solver keeps the same guarantees ───────────────────────────────

def test_spot_solver_roundtrips_and_gates_too():
    S, K, T, true_vol = 24500.0, 24500.0, 30 / 365, 0.17
    price = bs_price(S, K, T, R, true_vol, "CE")
    got = implied_volatility(price, S, K, T, R, "CE")
    assert got is not None and abs(got - true_vol) < 1e-5

    # Deep ITM on the spot solver is equally unidentifiable.
    deep = bs_price(S, 15000.0, T, R, true_vol, "CE")
    assert implied_volatility(deep, S, 15000.0, T, R, "CE") is None
