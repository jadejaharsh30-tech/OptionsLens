"""
Tests for the ported SHORT_BUILDUP rule (roadmap item 37).

The load-bearing test is `test_classification_matches_the_live_engine`: the
signal re-implements the engine's classifiers rather than importing them (the
engine reaches a broker client at module scope), so the equivalence has to be
checked, not claimed.

The rest pin the reconstruction of stage 1/stage 2 from the history window,
since that is where a stateful rule turned into a pure function could silently
drift from the thing it is supposed to measure.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_hours import IST, SessionPhase  # noqa: E402
from recorder.models import ChainRow, ChainSnapshot  # noqa: E402
from signals.base import Direction, SignalContext  # noqa: E402
from signals.oi_buildup import (  # noqa: E402
    SIGNAL_ID, atm_band, classify_premium_behavior, find_spike_bar,
    is_dual_side_writing, map_signal_to_trade, oi_speed_pct_per_min,
    score_confidence,
)
from signals.registry import get_signal  # noqa: E402

SPOT = 24500.0


def row(strike, opt="CE", oi=1000.0, ltp=100.0, oichp=0.0, volume=500.0) -> ChainRow:
    return ChainRow(strike=strike, option_type=opt, oi=oi, ltp=ltp,
                    oi_change_pct=oichp, volume=volume,
                    bid=ltp - 0.5, ask=ltp + 0.5)


def snap(rows, minute=0, spot=SPOT) -> ChainSnapshot:
    ts = (datetime(2026, 9, 24, 10, 0, tzinfo=IST) + timedelta(minutes=minute))
    return ChainSnapshot(
        ts=ts.isoformat(), session_date="2026-09-24",
        session_phase=SessionPhase.CONTINUOUS, symbol="NIFTY",
        expiry_date="01-10-2026", expiry_epoch=1790000000,
        spot=spot, rows=tuple(rows),
    )


def ladder(over=None):
    """
    A full ATM ladder. `over` is a {(strike, option_type): ChainRow} dict
    replacing individual rows — passed positionally because tuple keys cannot
    be keyword arguments.
    """
    over = over or {}
    out = []
    for k in (24400.0, 24500.0, 24600.0):
        for opt in ("CE", "PE"):
            out.append(over.get((k, opt), row(k, opt)))
    return out


def evaluate(history, current, params=None):
    spec = get_signal(SIGNAL_ID)
    ctx = SignalContext(snapshot=current, history=tuple(history), params=params or {})
    return spec.evaluate(ctx, params)


# ── Fidelity to the live engine ───────────────────────────────────────────────

def test_classification_matches_the_live_engine():
    """
    Every quadrant of the OI x premium matrix, checked against the engine's own
    functions. If these ever diverge, the backtest stops measuring the rule that
    actually runs.
    """
    from alert_engine.engine import (
        classify_premium_behavior as engine_classify,
        map_signal_to_trade as engine_map,
        score_confidence as engine_score,
    )

    for oi_up in (True, False):
        for ltp_now, ltp_base in ((120.0, 100.0), (80.0, 100.0), (100.0, 100.0)):
            assert (classify_premium_behavior(oi_up, ltp_now, ltp_base)
                    == engine_classify(oi_up, ltp_now, ltp_base))

    for opt in ("CE", "PE"):
        for behavior in ("SHORT_BUILDUP", "LONG_BUILDUP",
                         "SHORT_COVERING", "LONG_UNWINDING"):
            assert map_signal_to_trade(opt, behavior) == engine_map(opt, behavior)

    for oi_pct in (100.0, 500.0, 700.0, 2041.0):
        for speed in (0.0, 20.0, 50.0):
            for vol in (100.0, 1000.0):
                assert (score_confidence(oi_pct, speed, vol)
                        == engine_score(oi_pct, speed, vol))


def test_direction_inference_is_the_engines():
    """Call writing reads bearish, put writing bullish — the claim under test."""
    assert map_signal_to_trade("CE", "SHORT_BUILDUP") == ("BEARISH", "PE")
    assert map_signal_to_trade("PE", "SHORT_BUILDUP") == ("BULLISH", "CE")
    assert map_signal_to_trade("CE", "LONG_BUILDUP") == (None, None)


# ── Stage reconstruction from the history window ─────────────────────────────

def test_fires_on_oi_up_premium_down_after_a_spike():
    spiked = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1000, ltp=100.0,
                                            oichp=600.0)})
    now = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1400, ltp=85.0,
                                         oichp=650.0)})
    res = evaluate([snap(spiked, 0)], snap(now, 1))

    assert res.fired
    assert res.signal.direction is Direction.BEARISH
    assert res.signal.features["premium_behavior"] == "SHORT_BUILDUP"
    assert res.signal.features["trade_option"] == "PE"


def test_put_side_fires_bullish():
    spiked = ladder({(24500.0, "PE"): row(24500.0, "PE", oi=1000, ltp=90.0,
                                            oichp=800.0)})
    now = ladder({(24500.0, "PE"): row(24500.0, "PE", oi=1500, ltp=70.0,
                                         oichp=820.0)})
    res = evaluate([snap(spiked, 0)], snap(now, 1))
    assert res.fired and res.signal.direction is Direction.BULLISH


def test_no_spike_in_window_does_not_fire():
    quiet = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1000, ltp=100.0,
                                           oichp=50.0)})
    now = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1400, ltp=85.0,
                                         oichp=60.0)})
    res = evaluate([snap(quiet, 0)], snap(now, 1))
    assert not res.fired and "no confirmed buildup" in res.reason


def test_premium_rising_is_long_buildup_and_does_not_fire():
    spiked = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1000, ltp=100.0,
                                            oichp=600.0)})
    now = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1400, ltp=130.0,
                                         oichp=650.0)})
    assert not evaluate([snap(spiked, 0)], snap(now, 1)).fired


def test_oi_falling_is_not_buildup():
    spiked = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=2000, ltp=100.0,
                                            oichp=600.0)})
    now = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1500, ltp=85.0,
                                         oichp=610.0)})
    assert not evaluate([snap(spiked, 0)], snap(now, 1)).fired


def test_spike_outside_the_confirmation_window_expires():
    """The engine drops a pending spike after premium_confirm_polls."""
    spiked = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1000, ltp=100.0,
                                            oichp=600.0)})
    quiet  = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1100, ltp=95.0,
                                            oichp=20.0)})
    now    = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1400, ltp=85.0,
                                            oichp=25.0)})
    history = [snap(spiked, 0)] + [snap(quiet, i) for i in range(1, 6)]

    assert not evaluate(history, snap(now, 6),
                        {"premium_confirm_polls": 4}).fired
    # Widen the window and the same spike is back in range.
    assert evaluate(history, snap(now, 6),
                    {"premium_confirm_polls": 10}).fired


# ── Gates ─────────────────────────────────────────────────────────────────────

def test_volume_floor_rejects_thin_contracts():
    spiked = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1000, ltp=100.0,
                                            oichp=600.0, volume=10.0)})
    now = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1400, ltp=85.0,
                                         oichp=650.0, volume=10.0)})
    res = evaluate([snap(spiked, 0)], snap(now, 1), {"min_volume": 100.0})
    assert not res.fired
    assert res.features["below_volume"] >= 1


def test_dual_side_writing_is_suppressed():
    """Both legs spiking reads as event hedging, not a directional view."""
    spiked = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1000, ltp=100.0, oichp=600.0),
           (24500.0, "PE"): row(24500.0, "PE", oi=1000, ltp=90.0, oichp=600.0)})
    now = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1400, ltp=85.0, oichp=650.0),
           (24500.0, "PE"): row(24500.0, "PE", oi=1400, ltp=75.0, oichp=650.0)})

    assert not evaluate([snap(spiked, 0)], snap(now, 1),
                        {"suppress_dual_side": True}).fired
    assert evaluate([snap(spiked, 0)], snap(now, 1),
                    {"suppress_dual_side": False}).fired


def test_only_the_atm_band_is_examined():
    """A far strike spiking must not fire when the band is +/-1."""
    far = 24600.0
    spiked = ladder({(far, "CE"): row(far, "CE", oi=1000, ltp=50.0, oichp=900.0)})
    now = ladder({(far, "CE"): row(far, "CE", oi=1500, ltp=40.0, oichp=950.0)})

    # spot pinned to 24400 so 24600 is two steps away
    hist = [snap(spiked, 0, spot=24400.0)]
    assert not evaluate(hist, snap(now, 1, spot=24400.0),
                        {"strikes_either_side": 0}).fired
    assert evaluate(hist, snap(now, 1, spot=24400.0),
                    {"strikes_either_side": 2}).fired


def test_threshold_is_a_sweepable_parameter():
    spiked = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1000, ltp=100.0,
                                            oichp=300.0)})
    now = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1400, ltp=85.0,
                                         oichp=320.0)})
    assert not evaluate([snap(spiked, 0)], snap(now, 1),
                        {"oi_spike_threshold_pct": 500.0}).fired
    assert evaluate([snap(spiked, 0)], snap(now, 1),
                    {"oi_spike_threshold_pct": 250.0}).fired


# ── Helper units ──────────────────────────────────────────────────────────────

def test_atm_band_reads_the_chains_own_ladder():
    s = snap(ladder(), spot=24510.0)
    assert atm_band(s, 0) == {24500.0}
    assert atm_band(s, 1) == {24400.0, 24500.0, 24600.0}


def test_find_spike_bar_prefers_the_most_recent():
    old = snap(ladder({(24500.0, "CE"): row(24500.0, "CE", ltp=100.0, oichp=600.0)}), 0)
    new = snap(ladder({(24500.0, "CE"): row(24500.0, "CE", ltp=110.0, oichp=700.0)}), 1)
    found = find_spike_bar((old, new), 24500.0, "CE", 500.0)
    assert found.ltp == 110.0


def test_oi_speed_is_percent_per_minute():
    hist = (snap(ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1000.0)}), 0),
            snap(ladder({(24500.0, "CE"): row(24500.0, "CE", oi=1050.0)}), 1))
    # +20% over 2 one-minute bars -> 10%/min
    speed = oi_speed_pct_per_min(hist, row(24500.0, "CE", oi=1200.0),
                                 24500.0, "CE", interval_sec=60.0)
    assert abs(speed - 10.0) < 1e-6


def test_dual_side_needs_both_legs_present():
    s = snap([row(24500.0, "CE", oichp=600.0)])
    assert not is_dual_side_writing(s, 24500.0, 200.0)


# ── The scenario that motivated the port ──────────────────────────────────────

def test_reproduces_the_live_false_positive_shape():
    """
    NIFTY CE, huge oichp off a tiny settle base, premium decaying four days from
    expiry in a rising market. The rule calls this BEARISH. Recording the shape
    here so the backtest result can be read against a concrete case.
    """
    spiked = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=500, ltp=42.0,
                                            oichp=2041.0, volume=5000.0)})
    now = ladder({(24500.0, "CE"): row(24500.0, "CE", oi=900, ltp=38.5,
                                         oichp=2100.0, volume=6000.0)})
    # spot RISING while the signal says bearish
    res = evaluate([snap(spiked, 0, spot=24480.0)], snap(now, 1, spot=24530.0))

    assert res.fired
    assert res.signal.direction is Direction.BEARISH
    assert res.signal.features["confidence"] == "HIGH"
    assert res.signal.features["oi_pct_change"] == 2041.0
