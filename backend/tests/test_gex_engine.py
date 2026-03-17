import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gex_engine import compute_gex_for_strike, compute_net_gex_profile

def test_gex_positive_for_nonzero_inputs():
    result = compute_gex_for_strike(
        gamma=0.001, oi=1000, lot_size=75, spot=22000, option_type="CE"
    )
    assert result > 0

def test_gex_zero_for_zero_oi():
    result = compute_gex_for_strike(
        gamma=0.001, oi=0, lot_size=75, spot=22000, option_type="CE"
    )
    assert result == 0.0

def test_gex_zero_for_zero_gamma():
    result = compute_gex_for_strike(
        gamma=0.0, oi=1000, lot_size=75, spot=22000, option_type="CE"
    )
    assert result == 0.0

def test_net_gex_profile_structure():
    rows = [
        {"strike": 22000, "option_type": "CE", "gamma": 0.001, "oi": 1000},
        {"strike": 22000, "option_type": "PE", "gamma": 0.001, "oi": 800},
        {"strike": 22100, "option_type": "CE", "gamma": 0.0005,"oi": 500},
        {"strike": 22100, "option_type": "PE", "gamma": 0.0005,"oi": 600},
    ]
    profile = compute_net_gex_profile(rows, lot_size=75, spot=22000)
    assert len(profile) == 2
    strikes = [r["strike"] for r in profile]
    assert 22000 in strikes and 22100 in strikes

def test_net_gex_call_minus_put():
    rows = [
        {"strike": 22000, "option_type": "CE", "gamma": 0.001, "oi": 1000},
        {"strike": 22000, "option_type": "PE", "gamma": 0.001, "oi": 1000},
    ]
    profile = compute_net_gex_profile(rows, lot_size=75, spot=22000)
    assert abs(profile[0]["net_gex"]) < 1e-3

def test_net_gex_positive_when_calls_dominate():
    rows = [
        {"strike": 22000, "option_type": "CE", "gamma": 0.001, "oi": 2000},
        {"strike": 22000, "option_type": "PE", "gamma": 0.001, "oi": 500},
    ]
    profile = compute_net_gex_profile(rows, lot_size=75, spot=22000)
    assert profile[0]["net_gex"] > 0

def test_net_gex_negative_when_puts_dominate():
    rows = [
        {"strike": 22000, "option_type": "CE", "gamma": 0.001, "oi": 500},
        {"strike": 22000, "option_type": "PE", "gamma": 0.001, "oi": 2000},
    ]
    profile = compute_net_gex_profile(rows, lot_size=75, spot=22000)
    assert profile[0]["net_gex"] < 0

def test_profile_sorted_by_strike():
    rows = [
        {"strike": 22200, "option_type": "CE", "gamma": 0.001, "oi": 100},
        {"strike": 22000, "option_type": "CE", "gamma": 0.001, "oi": 200},
        {"strike": 22100, "option_type": "CE", "gamma": 0.001, "oi": 150},
    ]
    profile = compute_net_gex_profile(rows, lot_size=75, spot=22100)
    strikes = [r["strike"] for r in profile]
    assert strikes == sorted(strikes)

def test_profile_has_all_keys():
    rows = [{"strike": 22000, "option_type": "CE", "gamma": 0.001, "oi": 1000}]
    profile = compute_net_gex_profile(rows, lot_size=75, spot=22000)
    for key in ["strike", "call_gex", "put_gex", "net_gex"]:
        assert key in profile[0]


if __name__ == "__main__":
    tests = [
        test_gex_positive_for_nonzero_inputs,
        test_gex_zero_for_zero_oi,
        test_gex_zero_for_zero_gamma,
        test_net_gex_profile_structure,
        test_net_gex_call_minus_put,
        test_net_gex_positive_when_calls_dominate,
        test_net_gex_negative_when_puts_dominate,
        test_profile_sorted_by_strike,
        test_profile_has_all_keys,
    ]

    passed = failed = 0
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
    print("  🎉 All tests passed" if failed == 0 else f"  ⚠️  {failed} failed")
