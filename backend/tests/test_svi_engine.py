import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import math
from svi_engine import svi_total_variance, fit_svi_slice, svi_iv_at_moneyness

def test_svi_total_variance_positive():
    params = (0.04, 0.1, -0.3, 0.0, 0.1)  # a, b, rho, m, sigma
    for k in [-0.3, -0.1, 0.0, 0.1, 0.3]:
        w = svi_total_variance(k, *params)
        assert w > 0, f"w({k}) = {w} is not positive"

def test_svi_fit_recovers_smile():
    true_params = (0.04, 0.08, -0.4, 0.0, 0.12)
    moneyness_pts = [0.90, 0.93, 0.96, 0.99, 1.0, 1.01, 1.03, 1.06]
    T = 30 / 365
    iv_data = []
    for m in moneyness_pts:
        k = math.log(m)
        w = svi_total_variance(k, *true_params)
        iv = math.sqrt(w / T)
        iv_data.append((m, iv))
    fitted = fit_svi_slice(iv_data, T)
    assert fitted is not None, "Fit returned None on clean synthetic data"
    for m, true_iv in iv_data:
        fitted_iv = svi_iv_at_moneyness(m, T, fitted)
        assert abs(fitted_iv - true_iv) < 0.005, \
            f"At moneyness {m}: fitted {fitted_iv:.4f} vs true {true_iv:.4f}"

def test_svi_fit_returns_none_insufficient_points():
    iv_data = [(1.0, 0.15), (0.98, 0.17), (1.02, 0.16)]
    result = fit_svi_slice(iv_data, T=30/365)
    assert result is None

def test_svi_iv_at_moneyness_atm():
    params = (0.04, 0.08, -0.3, 0.0, 0.10)
    T = 30 / 365
    iv = svi_iv_at_moneyness(1.0, T, params)
    assert 0.05 < iv < 0.80, f"ATM IV {iv:.4f} is outside plausible range"

def test_svi_iv_smile_has_minimum_near_atm():
    params = (0.04, 0.08, -0.4, 0.0, 0.12)
    T = 30 / 365
    ivs = {m: svi_iv_at_moneyness(m, T, params)
           for m in [0.85, 0.90, 0.95, 1.0, 1.05, 1.10, 1.15]}
    min_m = min(ivs, key=ivs.get)
    assert 0.92 <= min_m <= 1.05, f"IV minimum at moneyness {min_m}, expected near 1.0"
