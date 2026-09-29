"""
Implied-correlation tests (roadmap item 35).

What would fail silently, and so is pinned here:

  - **The formula.** Checked against a basket whose correlation is known by
    construction, not against itself.
  - **A basket that changes size between dates.** Four stocks and five stocks
    give different numbers; a series mixing them is two definitions in one
    percentile. A date missing any member is a gap.
  - **A missing member blanking the whole series.** ICICIBANK absent from the
    import must shrink the basket once, visibly, not zero every date.
  - **Clipping.** ρ above 1 is information about the proxy, and clipping would
    flatten exactly the tail the signal ranks.
"""
import datetime as dt
import math
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dispersion import (  # noqa: E402
    build_dispersion_series, choose_basket, implied_correlation,
    load_dispersion_history, summarise,
)
from market_hours import SessionPhase  # noqa: E402
from recorder.models import ChainRow, ChainSnapshot  # noqa: E402
from signals.base import Direction, SignalContext  # noqa: E402
from signals.dispersion_signal import EXTRAS_KEY, SIGNAL_ID  # noqa: E402
from signals.registry import get_signal  # noqa: E402


def basket_vol(vols, rho):
    """Equal-weight basket vol for a given common pairwise correlation."""
    n = len(vols)
    w = 1.0 / n
    var = sum((w * v) ** 2 for v in vols)
    var += rho * sum(w * vi * w * vj for i, vi in enumerate(vols)
                     for j, vj in enumerate(vols) if i != j)
    return math.sqrt(var)


# ── The formula ───────────────────────────────────────────────────────────────

def test_recovers_the_correlation_that_built_the_index_vol():
    vols = [0.22, 0.25, 0.18, 0.30, 0.20]
    for rho in (0.1, 0.45, 0.8):
        assert abs(implied_correlation(basket_vol(vols, rho), vols) - rho) < 1e-12


def test_perfect_correlation_means_index_vol_is_the_average_member_vol():
    vols = [0.2, 0.3]
    assert abs(implied_correlation(0.25, vols) - 1.0) < 1e-12


def test_an_index_richer_than_any_correlation_allows_is_above_one_not_clipped():
    """Information about the proxy's bias; clipping would flatten the top tail."""
    assert implied_correlation(0.40, [0.2, 0.2, 0.2]) > 1.0


def test_an_index_below_the_diversification_floor_is_negative_not_clipped():
    """
    With few members the zero-correlation basket vol is high: two members at
    ~21% cannot go below ~14.9% at ρ = 0. An index at 14% therefore implies a
    negative correlation — a statement about the small basket, kept as-is.
    """
    assert implied_correlation(0.14, [0.22, 0.20]) < 0


def test_undefined_cross_sections_return_none():
    assert implied_correlation(0.2, [0.2]) is None           # one member
    assert implied_correlation(0.2, [0.2, None]) is None
    assert implied_correlation(0.0, [0.2, 0.3]) is None
    assert implied_correlation(0.2, [0.2, 0.0]) is None


# ── The basket ────────────────────────────────────────────────────────────────

def test_a_member_absent_from_the_import_is_excluded_not_fatal():
    index_dates = {f"d{i}" for i in range(100)}
    members = {"RELIANCE": set(index_dates), "TCS": set(index_dates),
               "ICICIBANK": set()}
    basket, excluded = choose_basket(index_dates, members)
    assert basket == ("RELIANCE", "TCS")
    assert excluded == {"ICICIBANK": 0.0}


def test_a_patchy_member_is_excluded_with_its_coverage_reported():
    index_dates = {f"d{i}" for i in range(100)}
    members = {"A": set(index_dates), "B": set(index_dates),
               "C": {f"d{i}" for i in range(30)}}
    basket, excluded = choose_basket(index_dates, members)
    assert basket == ("A", "B")
    assert excluded == {"C": 30.0}


def iv_rows(dates, iv):
    return [{"date": d, "iv": iv} for d in dates]


def test_a_date_missing_any_member_is_a_gap_not_a_smaller_basket():
    dates = ["2026-01-05", "2026-01-06", "2026-01-07"]
    index = iv_rows(dates, 0.15)
    members = {"A": iv_rows(dates, 0.20),
               "B": iv_rows(["2026-01-05", "2026-01-07"], 0.22)}   # 6th missing
    series = build_dispersion_series(index, members, ("A", "B"))
    assert [r["date"] for r in series] == ["2026-01-05", "2026-01-07"]


def test_join_is_on_date_not_position():
    index = [{"date": "2026-01-05", "iv": 0.15}, {"date": "2026-01-06", "iv": 0.30}]
    members = {"A": [{"date": "2026-01-06", "iv": 0.30}, {"date": "2026-01-05", "iv": 0.20}],
               "B": [{"date": "2026-01-05", "iv": 0.20}, {"date": "2026-01-06", "iv": 0.30}]}
    series = build_dispersion_series(index, members, ("A", "B"))
    by = {r["date"]: r for r in series}
    assert abs(by["2026-01-06"]["implied_corr"] - 1.0) < 1e-9       # 0.30 vs 0.30s
    assert by["2026-01-05"]["implied_corr"] < 1.0                   # 0.15 vs 0.20s


def test_realised_correlation_and_premium_ride_along_when_available():
    d = "2026-01-05"
    series = build_dispersion_series(
        [{"date": d, "iv": basket_vol([0.2, 0.3], 0.7)}],
        {"A": [{"date": d, "iv": 0.2}], "B": [{"date": d, "iv": 0.3}]},
        ("A", "B"),
        index_rv=[{"date": d, "rv": basket_vol([0.2, 0.3], 0.5)}],
        member_rv={"A": [{"date": d, "rv": 0.2}], "B": [{"date": d, "rv": 0.3}]})
    r = series[0]
    assert abs(r["implied_corr"] - 0.7) < 1e-6
    assert abs(r["realised_corr"] - 0.5) < 1e-6
    assert abs(r["corr_premium"] - 0.2) < 1e-6


# ── The loader ────────────────────────────────────────────────────────────────

def test_loader_builds_from_the_app_store_and_reports_its_basket():
    from snapshot_store import init_db, write_atm_iv, write_spot_price

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "app.db")
        init_db(db)
        base = dt.date(2026, 3, 2)
        for i in range(30):
            d = (base + dt.timedelta(days=i)).isoformat()
            for sym, iv in (("NIFTY", 0.18), ("RELIANCE", 0.22), ("TCS", 0.20)):
                for offset in (20, 40):
                    exp = (base + dt.timedelta(days=i + offset)).strftime("%d-%m-%Y")
                    write_atm_iv(db, d, sym, exp, iv)
                write_spot_price(db, d, sym, 1000.0 * (1 + 0.001 * (i % 5)))

        series, info = load_dispersion_history(
            db, "NIFTY", basket=("RELIANCE", "TCS", "ICICIBANK"))

    assert info["basket"] == ["RELIANCE", "TCS"]
    assert info["excluded"] == {"ICICIBANK": 0.0}
    assert len(series) == 30
    assert all(0 < r["implied_corr"] < 1 for r in series)
    s = summarise(series, info)
    assert s["observations"] == 30 and s["basket"] == ["RELIANCE", "TCS"]


def test_a_symbol_with_no_basket_explains_itself():
    with tempfile.TemporaryDirectory() as tmp:
        from snapshot_store import init_db
        db = os.path.join(tmp, "app.db")
        init_db(db)
        series, info = load_dispersion_history(db, "RELIANCE")
    assert series == []
    assert "no configured basket" in summarise(series, info)["note"]


# ── The signal ────────────────────────────────────────────────────────────────

def snapshot(d):
    return ChainSnapshot(
        ts=f"{d}T15:30:00+05:30", session_date=d,
        session_phase=SessionPhase.END_OF_DAY, symbol="NIFTY",
        expiry_date="27-10-2026", expiry_epoch=1, spot=24000.0,
        rows=(ChainRow(strike=24000.0, option_type="CE", ltp=100.0),))


def series_ending(d, today_corr, n=120):
    day = dt.date.fromisoformat(d)
    out = [{"date": (day - dt.timedelta(days=i)).isoformat(), "index_iv": 0.14,
            "mean_member_iv": 0.22, "implied_corr": 0.4 + (i % 21) * 0.01}
           for i in range(n, 0, -1)]
    out.append({"date": d, "index_iv": 0.14, "mean_member_iv": 0.22,
                "implied_corr": today_corr})
    return out


def evaluate(d, series, params=None):
    spec = get_signal(SIGNAL_ID)
    ctx = SignalContext(snapshot=snapshot(d), history=(), params=params or {},
                        extras={EXTRAS_KEY: series})
    return spec.evaluate(ctx, params)


def test_rich_correlation_sells_index_vol():
    d = "2026-06-15"
    res = evaluate(d, series_ending(d, 0.9))
    assert res.fired and res.signal.direction is Direction.SHORT_VOL


def test_cheap_correlation_buys_index_vol():
    d = "2026-06-15"
    res = evaluate(d, series_ending(d, 0.1))
    assert res.fired and res.signal.direction is Direction.LONG_VOL


def test_middling_correlation_does_not_fire():
    d = "2026-06-15"
    res = evaluate(d, series_ending(d, 0.5))
    assert not res.fired and "percentile" in res.reason


def test_todays_reading_is_excluded_from_its_own_percentile():
    d = "2026-06-15"
    res = evaluate(d, series_ending(d, 0.99))
    assert res.signal.features["history_size"] == 120
    assert res.signal.features["corr_percentile"] == 100.0


def test_no_series_and_no_row_today_both_skip_with_reasons():
    d = "2026-06-15"
    assert "extras" in evaluate(d, []).reason
    assert "no implied correlation" in evaluate(d, series_ending(d, 0.9)[:-1]).reason


def test_thin_history_skips():
    d = "2026-06-15"
    res = evaluate(d, series_ending(d, 0.9, n=10))
    assert not res.fired and "10 prior" in res.reason
