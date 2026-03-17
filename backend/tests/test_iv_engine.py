import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import math
from iv_engine import bs_price, bs_vega, implied_volatility, greeks

# ── Black-Scholes price tests ─────────────────────────────────────────────────

def test_bs_call_price_known_value():
    # Known BS call price: S=100, K=100, T=1yr, r=5%, sigma=20%
    # Expected ≈ 10.45 (from Hull textbook)
    price = bs_price(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="CE")
    assert abs(price - 10.45) < 0.05, f"Expected ~10.45, got {price:.4f}"

def test_bs_put_price_put_call_parity():
    # Put-call parity: C - P = S - K*e^(-rT)
    S, K, T, r, sigma = 100, 100, 1.0, 0.05, 0.20
    call = bs_price(S, K, T, r, sigma, "CE")
    put  = bs_price(S, K, T, r, sigma, "PE")
    lhs  = call - put
    rhs  = S - K * math.exp(-r * T)
    assert abs(lhs - rhs) < 1e-6, f"Put-call parity failed: {lhs:.6f} != {rhs:.6f}"

def test_bs_deep_itm_call():
    # Deep ITM call ≈ intrinsic value (S - K*e^{-rT})
    price = bs_price(S=200, K=100, T=0.1, r=0.065, sigma=0.20, option_type="CE")
    intrinsic = 200 - 100 * math.exp(-0.065 * 0.1)
    assert price > intrinsic * 0.95

def test_bs_zero_time_to_expiry_call():
    # At expiry, call = max(S-K, 0)
    price = bs_price(S=105, K=100, T=1e-9, r=0.065, sigma=0.20, option_type="CE")
    assert abs(price - 5.0) < 0.01

def test_bs_zero_time_to_expiry_put_otm():
    # At expiry, OTM put = 0
    price = bs_price(S=105, K=100, T=1e-9, r=0.065, sigma=0.20, option_type="PE")
    assert price < 0.01

# ── Vega tests ────────────────────────────────────────────────────────────────

def test_vega_is_positive():
    v = bs_vega(S=100, K=100, T=1.0, r=0.05, sigma=0.20)
    assert v > 0

def test_vega_atm_is_highest():
    # ATM vega > ITM vega > deep OTM vega
    v_atm  = bs_vega(S=100, K=100,  T=1.0, r=0.05, sigma=0.20)
    v_itm  = bs_vega(S=100, K=90,   T=1.0, r=0.05, sigma=0.20)
    v_dotm = bs_vega(S=100, K=150,  T=1.0, r=0.05, sigma=0.20)
    assert v_atm > v_itm
    assert v_atm > v_dotm

# ── IV solver tests ───────────────────────────────────────────────────────────

def test_iv_solver_roundtrip_call():
    # Price a call at known IV, then recover IV from price
    true_iv = 0.25
    S, K, T, r = 22000, 22000, 30/365, 0.065
    market_price = bs_price(S, K, T, r, true_iv, "CE")
    recovered_iv = implied_volatility(market_price, S, K, T, r, "CE")
    assert recovered_iv is not None
    assert abs(recovered_iv - true_iv) < 1e-5, f"Expected {true_iv}, got {recovered_iv}"

def test_iv_solver_roundtrip_put():
    true_iv = 0.18
    S, K, T, r = 22000, 21500, 15/365, 0.065
    market_price = bs_price(S, K, T, r, true_iv, "PE")
    recovered_iv = implied_volatility(market_price, S, K, T, r, "PE")
    assert recovered_iv is not None
    assert abs(recovered_iv - true_iv) < 1e-5

def test_iv_solver_returns_none_for_zero_price():
    result = implied_volatility(0.0, 22000, 22000, 30/365, 0.065, "CE")
    assert result is None

def test_iv_solver_returns_none_for_negative_price():
    result = implied_volatility(-5.0, 22000, 22000, 30/365, 0.065, "CE")
    assert result is None

def test_iv_solver_handles_deep_otm():
    # Very cheap OTM option — solver should not raise, return None if unconverged
    result = implied_volatility(0.05, 22000, 30000, 5/365, 0.065, "CE")
    assert result is None or result > 0

# ── Greeks tests ──────────────────────────────────────────────────────────────

def test_call_delta_between_0_and_1():
    g = greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="CE")
    assert 0 < g["delta"] < 1

def test_put_delta_between_minus1_and_0():
    g = greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="PE")
    assert -1 < g["delta"] < 0

def test_call_put_delta_sum_near_one():
    # call_delta - put_delta ≈ 1 (from put-call parity)
    g_call = greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="CE")
    g_put  = greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="PE")
    assert abs(g_call["delta"] - g_put["delta"] - 1.0) < 1e-6

def test_gamma_is_positive():
    g = greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="CE")
    assert g["gamma"] > 0

def test_theta_is_negative_for_long_option():
    g = greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="CE")
    assert g["theta"] < 0

def test_vega_same_for_call_and_put():
    g_call = greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="CE")
    g_put  = greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="PE")
    assert abs(g_call["vega"] - g_put["vega"]) < 1e-6

def test_greeks_returns_all_keys():
    g = greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="CE")
    for key in ["delta", "gamma", "vega", "theta", "rho"]:
        assert key in g, f"Missing key: {key}"

# ── Run all tests manually (no pytest needed) ─────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_bs_call_price_known_value,
        test_bs_put_price_put_call_parity,
        test_bs_deep_itm_call,
        test_bs_zero_time_to_expiry_call,
        test_bs_zero_time_to_expiry_put_otm,
        test_vega_is_positive,
        test_vega_atm_is_highest,
        test_iv_solver_roundtrip_call,
        test_iv_solver_roundtrip_put,
        test_iv_solver_returns_none_for_zero_price,
        test_iv_solver_returns_none_for_negative_price,
        test_iv_solver_handles_deep_otm,
        test_call_delta_between_0_and_1,
        test_put_delta_between_minus1_and_0,
        test_call_put_delta_sum_near_one,
        test_gamma_is_positive,
        test_theta_is_negative_for_long_option,
        test_vega_same_for_call_and_put,
        test_greeks_returns_all_keys,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ FAIL  {t.__name__} — {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"  {passed}/{passed+failed} tests passed")
    if failed == 0:
        print("  🎉 All tests passed")
    else:
        print(f"  ⚠️  {failed} test(s) failed")
