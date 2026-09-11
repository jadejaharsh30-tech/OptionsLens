"""
End-to-end backtest pipeline test.

Builds a synthetic recorded session, replays a registered signal over it, labels
forward returns, samples a matched null and compares. This is the test that
proves the central architectural claim: the same signal code runs live and in
backtest, because both consume ChainSnapshot and nothing else.

It also pins the two properties most easily lost in refactoring:
  - no look-ahead (a signal never sees its own bar in history)
  - non-fires are recorded, so the denominator survives
"""
import math
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.benchmark import compare, build_null_labels, sample_null_entries  # noqa: E402
from backtest.costs import (  # noqa: E402
    CostConfig, FillModel, execute_leg, fill_price, net_pnl, round_trip_cost,
)
from backtest.labels import compute_forward_returns, horizon_keys  # noqa: E402
from backtest.metrics import horizon_stats, interpret, BacktestStats  # noqa: E402
from backtest.replay import replay_session  # noqa: E402
from backtest.walkforward import (  # noqa: E402
    chronological_split, describe_data_sufficiency, rolling_splits,
)
from iv_engine import black76_price  # noqa: E402
from market_hours import IST, SessionPhase  # noqa: E402
from recorder.models import ChainRow, ChainSnapshot  # noqa: E402
from recorder.store import init_db, write_snapshot  # noqa: E402
from signals.base import Direction, Signal, SignalContext, SignalResult  # noqa: E402
from signals.registry import get_signal, list_signals, register_signal  # noqa: E402
from signals.store import (  # noqa: E402
    evaluation_summary, init_db as init_signals_db, iter_evaluations,
    write_evaluations,
)

R, SIGMA = 0.065, 0.16


def build_snapshot(ts: str, spot: float, expiry: str = "17-09-2026",
                   symbol: str = "NIFTY") -> ChainSnapshot:
    """A realistic chain: consistent Black-76 prices around a moving forward."""
    T = 6 / 365
    forward = spot * math.exp(R * T)
    rows = []
    for k in range(int(spot) - 500, int(spot) + 501, 100):
        strike = float(k)
        for opt in ("CE", "PE"):
            px = black76_price(forward, strike, T, R, SIGMA, opt)
            rows.append(ChainRow(
                strike=strike, option_type=opt,
                ltp=round(px, 2), bid=round(px - 0.5, 2), ask=round(px + 0.5, 2),
                oi=1000.0 + abs(k - spot), volume=500.0,
            ))
    return ChainSnapshot(
        ts=ts, session_date=ts[:10], session_phase=SessionPhase.CONTINUOUS,
        symbol=symbol, expiry_date=expiry, expiry_epoch=1789564800,
        spot=spot, rows=tuple(rows),
    )


def seed_session(db: str, session_date: str, n: int = 90,
                 drift: float = 0.5, symbol: str = "NIFTY"):
    """Write one synthetic session of minute snapshots."""
    start = datetime.fromisoformat(f"{session_date}T09:15:00").replace(tzinfo=IST)
    for i in range(n):
        ts = (start + timedelta(minutes=i)).isoformat()
        write_snapshot(build_snapshot(ts, 24500.0 + i * drift, symbol=symbol), db)


def temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    init_signals_db(path)
    return path


# ── A deterministic signal, registered once for these tests ───────────────────

@register_signal("always_long_test", version=1,
                 default_params={"min_spot": 0.0}, min_history=2)
def _always_long(ctx: SignalContext) -> SignalResult:
    """Fires whenever spot exceeds a parameter — deterministic and sweepable."""
    spot = ctx.snapshot.spot
    if spot < ctx.param("min_spot", 0.0):
        return SignalResult.skip("always_long_test", 1, ctx,
                                 f"spot {spot} below min", {"spot": spot})
    return SignalResult.fire(Signal(
        signal_id="always_long_test", version=1, ts=ctx.ts,
        symbol=ctx.symbol, direction=Direction.BULLISH, strength=0.5,
        features={"spot": spot, "history_len": len(ctx.history)},
    ))


# ── Replay ────────────────────────────────────────────────────────────────────

def test_replay_evaluates_every_snapshot():
    db = temp_db()
    try:
        seed_session(db, "2026-09-10", n=30)
        spec = get_signal("always_long_test", 1)
        r = replay_session(spec, "NIFTY", "2026-09-10", db_path=db)
        assert len(r.results) == 30
        assert len(r.timestamps) == 30 and len(r.spots) == 30
    finally:
        os.unlink(db)


def test_no_lookahead_history_excludes_current_bar():
    """
    The structural guarantee. At bar i the signal must see exactly i prior
    snapshots — never itself, never anything later.
    """
    db = temp_db()
    try:
        seed_session(db, "2026-09-10", n=20)
        spec = get_signal("always_long_test", 1)
        r = replay_session(spec, "NIFTY", "2026-09-10", db_path=db)

        for i, res in enumerate(r.results):
            if res.fired:
                assert res.signal.features["history_len"] == i
    finally:
        os.unlink(db)


def test_min_history_produces_warmup_skips_not_fires():
    db = temp_db()
    try:
        seed_session(db, "2026-09-10", n=10)
        spec = get_signal("always_long_test", 1)
        r = replay_session(spec, "NIFTY", "2026-09-10", db_path=db)

        assert not r.results[0].fired and "warming up" in r.results[0].reason
        assert not r.results[1].fired
        assert r.results[2].fired
    finally:
        os.unlink(db)


def test_params_override_defaults_and_change_firing():
    db = temp_db()
    try:
        seed_session(db, "2026-09-10", n=20, drift=1.0)
        spec = get_signal("always_long_test", 1)

        loose = replay_session(spec, "NIFTY", "2026-09-10",
                               params={"min_spot": 0.0}, db_path=db)
        tight = replay_session(spec, "NIFTY", "2026-09-10",
                               params={"min_spot": 24510.0}, db_path=db)
        assert len(loose.fired) > len(tight.fired)
    finally:
        os.unlink(db)


# ── Labels ────────────────────────────────────────────────────────────────────

def test_forward_returns_measure_the_right_direction():
    timestamps = [(datetime(2026, 9, 10, 9, 15, tzinfo=IST)
                   + timedelta(minutes=i)).isoformat() for i in range(40)]
    spots = [24500.0 + i for i in range(40)]   # steadily rising

    long_lab = compute_forward_returns(0, timestamps, spots, "NIFTY", direction=1)
    assert long_lab.returns_bps["5m"] > 0

    short_lab = compute_forward_returns(0, timestamps, spots, "NIFTY", direction=-1)
    assert short_lab.returns_bps["5m"] == -long_lab.returns_bps["5m"]


def test_horizon_beyond_available_data_is_none_not_truncated():
    """A 60m label built from 10 minutes of data would be a different statistic."""
    timestamps = [(datetime(2026, 9, 10, 9, 15, tzinfo=IST)
                   + timedelta(minutes=i)).isoformat() for i in range(10)]
    spots = [24500.0 + i for i in range(10)]
    lab = compute_forward_returns(0, timestamps, spots, "NIFTY")
    assert lab.returns_bps["5m"] is not None
    assert lab.returns_bps["60m"] is None


def test_mae_mfe_bracket_the_realised_return():
    timestamps = [(datetime(2026, 9, 10, 9, 15, tzinfo=IST)
                   + timedelta(minutes=i)).isoformat() for i in range(30)]
    spots = [24500, 24450, 24600, 24520] + [24520.0] * 26  # dips then rallies
    lab = compute_forward_returns(0, timestamps, spots, "NIFTY")
    assert lab.mae_bps["5m"] < 0 < lab.mfe_bps["5m"]
    assert lab.mae_bps["5m"] <= lab.returns_bps["5m"] <= lab.mfe_bps["5m"]


# ── Costs ─────────────────────────────────────────────────────────────────────

def test_spread_fill_is_worse_than_mid_on_both_sides():
    cfg_spread = CostConfig(fill_model=FillModel.SPREAD, slippage_ticks=0)
    cfg_mid    = CostConfig(fill_model=FillModel.MID, slippage_ticks=0)

    buy_spread = fill_price(100.0, 102.0, 101.0, True, cfg_spread)
    buy_mid    = fill_price(100.0, 102.0, 101.0, True, cfg_mid)
    assert buy_spread > buy_mid

    sell_spread = fill_price(100.0, 102.0, 101.0, False, cfg_spread)
    sell_mid    = fill_price(100.0, 102.0, 101.0, False, cfg_mid)
    assert sell_spread < sell_mid


def test_one_sided_book_cannot_be_filled():
    """Inventing a fill on an empty book is how backtests manufacture profit."""
    cfg = CostConfig()
    assert fill_price(0.0, 102.0, 101.0, True, cfg) is None
    assert fill_price(100.0, 0.0, 101.0, False, cfg) is None
    assert execute_leg(0.0, 0.0, 0.0, 75, True, cfg) is None


def test_round_trip_cost_separates_spread_from_charges():
    cfg = CostConfig()
    entry = execute_leg(100.0, 102.0, 101.0, 75, True, cfg)
    exit_ = execute_leg(110.0, 112.0, 111.0, 75, False, cfg)
    breakdown = round_trip_cost(entry, exit_)

    assert breakdown["spread_and_slippage"] > 0
    assert breakdown["statutory_and_brokerage"] > 0
    assert abs(breakdown["total"] - (breakdown["spread_and_slippage"]
               + breakdown["statutory_and_brokerage"])) < 0.01


def test_costs_can_turn_a_winning_trade_into_a_loss():
    """The reason the cost model exists at all."""
    cfg = CostConfig()
    entry = execute_leg(100.0, 102.0, 101.0, 75, True, cfg)
    exit_ = execute_leg(100.5, 102.5, 101.5, 75, False, cfg)  # +0.5 move
    assert net_pnl(entry, exit_) < 0


def test_stt_applies_only_to_the_sell_leg():
    cfg = CostConfig()
    buy  = execute_leg(100.0, 102.0, 101.0, 75, True, cfg)
    sell = execute_leg(100.0, 102.0, 101.0, 75, False, cfg)
    assert sell.charges > buy.charges


# ── Metrics and benchmark ─────────────────────────────────────────────────────

def test_horizon_stats_drops_unlabelled_rather_than_zeroing():
    s = horizon_stats("5m", [10.0, -5.0, None, 20.0, None])
    assert s.n == 3
    assert s.hit_rate_pct == round(2 / 3 * 100, 2)


def test_t_stat_flags_noise():
    noisy = horizon_stats("5m", [5.0, -5.0, 4.0, -4.0, 6.0, -6.0] * 10)
    assert abs(noisy.t_stat) < 2.0

    consistent = horizon_stats("5m", [5.0, 5.5, 4.5, 5.2, 4.8] * 12)
    assert consistent.t_stat > 2.0


def test_interpret_warns_on_thin_samples():
    stats = BacktestStats(signal_id="x", version=1, n_signals=3)
    stats.by_horizon = {"5m": horizon_stats("5m", [1.0, 2.0, 3.0])}
    notes = interpret(stats, min_sample=30)
    assert any("below the 30 minimum" in n for n in notes)


def test_null_is_matched_on_time_of_day():
    """The null must draw from the same clock times the signal fired at."""
    series = {}
    for d in ("2026-09-08", "2026-09-09", "2026-09-10"):
        ts = [(datetime.fromisoformat(f"{d}T09:15:00").replace(tzinfo=IST)
               + timedelta(minutes=i)).isoformat() for i in range(60)]
        series[d] = (ts, [24500.0 + i for i in range(60)])

    signal_ts = [series["2026-09-10"][0][30]]   # 09:45
    picks = sample_null_entries(signal_ts, series, samples_per_signal=9, seed=1)

    assert len(picks) == 9
    assert all(ts[11:16] == "09:45" for _, ts, _ in picks)


def test_compare_produces_a_verdict_per_horizon():
    series = {}
    for d in ("2026-09-08", "2026-09-09", "2026-09-10"):
        ts = [(datetime.fromisoformat(f"{d}T09:15:00").replace(tzinfo=IST)
               + timedelta(minutes=i)).isoformat() for i in range(80)]
        series[d] = (ts, [24500.0 + i * 0.5 for i in range(80)])

    sig_labels = [compute_forward_returns(20, *series["2026-09-10"], "NIFTY")]
    picks = sample_null_entries([series["2026-09-10"][0][20]], series,
                                samples_per_signal=5, seed=1)
    null_labels = build_null_labels(picks, series, "NIFTY")

    result = compare(sig_labels, null_labels)
    assert set(result.keys()) == set(horizon_keys())
    # One signal is nowhere near enough to conclude anything, and the verdict
    # must say so rather than reporting a number.
    assert result["5m"].verdict() == "INSUFFICIENT_DATA"


# ── Evaluation log ────────────────────────────────────────────────────────────

def test_non_fires_are_persisted_with_reasons():
    db = temp_db()
    try:
        seed_session(db, "2026-09-10", n=20, drift=1.0)
        spec = get_signal("always_long_test", 1)
        r = replay_session(spec, "NIFTY", "2026-09-10",
                           params={"min_spot": 24510.0}, db_path=db)

        write_evaluations(r.results, run_id="test-run", db_path=db)
        stored = list(iter_evaluations("always_long_test", db_path=db))

        assert len(stored) == 20
        assert any(not e["fired"] for e in stored)
        assert any(e["reason"] and "below min" in e["reason"] for e in stored)

        summary = evaluation_summary("always_long_test", db_path=db)
        assert summary["evaluations"] == 20
        assert summary["fired"] + sum(
            r["count"] for r in summary["skip_reasons"]
        ) == 20
    finally:
        os.unlink(db)


# ── Walk-forward ──────────────────────────────────────────────────────────────

def test_splits_are_chronological_and_non_overlapping():
    dates = [f"2026-09-{d:02d}" for d in range(1, 21)]
    splits = rolling_splits(dates, train_days=10, test_days=5)

    assert len(splits) >= 2
    for s in splits:
        assert max(s.train) < min(s.test)      # test strictly follows train
    # Default step equals test_days, so test windows tile without overlap.
    all_test = [d for s in splits for d in s.test]
    assert len(all_test) == len(set(all_test))


def test_data_sufficiency_says_no_when_series_is_short():
    info = describe_data_sufficiency(["2026-09-01", "2026-09-02"], 20, 10)
    assert info["sufficient"] is False
    assert "need 30" in info["note"]
    assert chronological_split(["2026-09-01", "2026-09-02"]) is None


# ── The registered production signal ──────────────────────────────────────────

def test_gex_regime_is_registered_and_runs_on_a_real_chain():
    import signals.library  # noqa: F401  (registers it)
    db = temp_db()
    try:
        seed_session(db, "2026-09-10", n=15)
        spec = get_signal("gex_regime")
        r = replay_session(spec, "NIFTY", "2026-09-10", db_path=db)

        assert len(r.results) == 15
        # Every result must be interpretable: fired, or skipped with a reason.
        for res in r.results:
            assert res.fired or res.reason
    finally:
        os.unlink(db)


def test_registry_rejects_duplicate_registration():
    import pytest
    with pytest.raises(ValueError, match="already registered"):
        register_signal("always_long_test", version=1)(lambda ctx: None)


def test_signals_are_discoverable_with_versions():
    keys = [s.key for s in list_signals()]
    assert "always_long_test.v1" in keys
