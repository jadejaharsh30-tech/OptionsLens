"""
Overlap-aware significance tests (the fix for the 35.5% false-EDGE rate).

The first real NIFTY runs reported EDGE through a Welch t-test that counts
overlapping 20-session outcomes, from signals firing in long runs, as
independent observations. These tests pin the replacement:

  - on worlds with NO edge it rarely says EDGE, while the old test on the SAME
    worlds usually does — the reason for the change, kept executable;
  - it still finds an edge that is really there;
  - it refuses to give a verdict it cannot resolve (too few offsets);
  - per-direction results separate a losing side from a winning aggregate.

Seeded and deterministic: the simulated worlds are the same on every run.
"""
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.benchmark import _welch_t  # noqa: E402
from backtest.significance import (  # noqa: E402
    MIN_OFFSETS, circular_shift_test, edge_statistic, horizon_sessions,
)


def world(rng, n=300, h=10, warm=40, phi=0.97, edge=0.0):
    """
    A persistent percentile signal and overlapping h-session outcomes. With
    edge=0 the signal's state is independent of the outcome noise, so any
    "edge" found is a false positive.
    """
    state, x = [], 0.0
    for _ in range(n + h):
        x = phi * x + rng.gauss(0, 1)
        state.append(x)
    noise = [rng.gauss(0, 1) for _ in range(n + h)]
    signs, outcomes = [], []
    for i in range(n):
        if i < warm:
            signs.append(0)
        else:
            past = state[:i]
            pct = sum(1 for p in past if p < state[i]) / len(past) * 100
            signs.append(1 if pct >= 80 else (-1 if pct <= 20 else 0))
        y = sum(noise[i + 1:i + 1 + h]) / math.sqrt(h) + 0.3
        outcomes.append(y + edge * signs[-1] if i < n - h else None)
    return signs, outcomes


def welch_on(signs, outcomes):
    """The old test: fired outcomes vs every outcome, all counted independent."""
    fired = [s * v for s, v in zip(signs, outcomes) if s and v is not None]
    base = [v for v in outcomes if v is not None]
    ybar = sum(base) / len(base)
    null = [s * ybar for s, v in zip(signs, outcomes) if s and v is not None]
    def st(xs):
        m = sum(xs) / len(xs)
        return m, math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1)), len(xs)
    # Null spread from the unconditional outcome distribution.
    mb, sb, _ = st(base)
    mf, sf, nf = st(fired)
    return _welch_t(mf, sf, nf, sum(null) / len(null), sb, len(base))


def test_no_edge_worlds_rarely_pass_the_shift_test_but_often_pass_the_t_test():
    rng = random.Random(2026)
    shift_hits = welch_hits = worlds = 0
    for _ in range(60):
        signs, outcomes = world(rng)
        t = circular_shift_test(signs, outcomes, 10)
        if t.p_value is None:
            continue
        worlds += 1
        shift_hits += t.p_value <= 0.05
        w = welch_on(signs, outcomes)
        welch_hits += w is not None and abs(w) >= 2.0
    assert worlds >= 50
    assert shift_hits / worlds <= 0.15          # ~5% target, loose for 60 worlds
    assert welch_hits / worlds >= 0.20          # the old test's failure, pinned
    assert welch_hits > 2 * shift_hits


def test_a_real_edge_is_still_detected():
    rng = random.Random(7)
    detected = 0
    for _ in range(20):
        signs, outcomes = world(rng, edge=1.0)
        t = circular_shift_test(signs, outcomes, 10)
        detected += t.p_value is not None and t.p_value <= 0.05 and t.edge > 0
    assert detected >= 14                        # most of the time, not always


def test_edge_statistic_is_signal_mean_minus_the_same_mix_on_an_average_day():
    outcomes = [1.0, 2.0, 3.0, 4.0]              # mean 2.5
    assert edge_statistic([0, 0, 0, 1], outcomes) == 1.5
    assert edge_statistic([-1, 0, 0, 0], outcomes) == 1.5    # short the low day
    assert edge_statistic([0, 0, 0, 0], outcomes) is None


def test_sessions_without_an_outcome_drop_out():
    assert edge_statistic([1, 1], [None, 3.0], mean_outcome=2.0) == 1.0


def test_shifts_never_overlap_the_original_outcomes():
    n, h = 100, 20
    t = circular_shift_test([1] * 10 + [0] * 90, [0.0] * 90 + [None] * 10, h)
    assert t.n_offsets == n - 2 * h + 1


def test_too_few_offsets_gives_no_p_value_rather_than_a_false_no_edge():
    """With k offsets the smallest p is 1/(k+1); below 19 it cannot reach 0.05."""
    t = circular_shift_test([1, 0] * 15, [0.1] * 30, 10)
    assert t.n_offsets < MIN_OFFSETS
    assert t.p_value is None


def test_mismatched_lengths_are_an_error():
    import pytest
    with pytest.raises(ValueError):
        circular_shift_test([1, 0], [0.1], 1)


def test_horizon_keys_parse_to_sessions():
    assert horizon_sessions("20d") == 20
    assert horizon_sessions("5d_vol") == 5
    assert horizon_sessions("15m") is None
    assert horizon_sessions("eod") is None


# ── Per-direction results ─────────────────────────────────────────────────────

def test_a_losing_side_is_reported_as_losing_even_when_it_beats_its_baseline():
    """
    The dispersion case: long-vol fires lose (implied usually exceeds realised)
    but lose less than long vol on an average day. The table must show both the
    negative mean and the positive edge, not only the aggregate.
    """
    from backtest.engine import _by_direction

    rng = random.Random(3)
    n = 200
    outcomes = {"20d_vol": [1.0 + rng.gauss(0, 0.2) for _ in range(n)]}
    signs = [0] * n
    for i in range(0, n, 4):
        signs[i] = -1                                       # LONG_VOL
        outcomes["20d_vol"][i] = 0.6                        # IV beat RV by less
    out = _by_direction(signs, outcomes, {1: "SHORT_VOL", -1: "LONG_VOL"})

    assert "SHORT_VOL" not in out                           # never fired that way
    lv = out["LONG_VOL"]["horizons"]["20d_vol"]
    assert lv["mean"] < 0                                   # the trade lost...
    assert lv["baseline"] < lv["mean"]                      # ...less than average
    assert lv["edge"] > 0
    assert out["LONG_VOL"]["fires"] == 50


# ── Through the engine ────────────────────────────────────────────────────────

def test_daily_runs_report_the_shift_test_and_intraday_runs_warn():
    import os
    import tempfile

    from backtest.engine import run_backtest
    from market_hours import SessionPhase
    from recorder.models import ChainRow, ChainSnapshot
    from recorder.store import init_db, write_snapshot
    from signals.base import Direction, Signal, SignalResult
    from signals.registry import _REGISTRY, register_signal

    if "sig_probe_runs.v1" not in _REGISTRY:
        @register_signal("sig_probe_runs", version=1, min_history=0)
        def _probe(ctx):
            # Fires in runs: two weeks on, two weeks off, both directions.
            day = int(ctx.snapshot.session_date[-2:])
            if day > 14:
                return SignalResult.skip("sig_probe_runs", 1, ctx, "off")
            d = Direction.BULLISH if ctx.snapshot.session_date[5:7] in ("01", "03", "05") \
                else Direction.BEARISH
            return SignalResult.fire(Signal(
                signal_id="sig_probe_runs", version=1, ts=ctx.ts,
                symbol=ctx.symbol, direction=d, strength=0.5))

    import datetime as dt
    days, d = [], dt.date(2025, 1, 1)
    while len(days) < 160:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "eod.db")
        init_db(db)
        spot = 24000.0
        for i, day in enumerate(days):
            spot *= 1 + 0.006 * math.sin(i * 1.3)
            write_snapshot(ChainSnapshot(
                ts=f"{day}T15:30:00+05:30", session_date=day,
                session_phase=SessionPhase.END_OF_DAY, symbol="NIFTY",
                expiry_date="30-12-2026", expiry_epoch=1, spot=spot,
                rows=(ChainRow(strike=24000.0, option_type="CE", ltp=100.0),)), db)
        run = run_backtest("sig_probe_runs", "NIFTY", db_path=db,
                           persist_evaluations=False).to_dict()

    assert run["label_mode"] == "daily"
    for h, row in run["horizons"].items():
        assert row["test"] == "circular_shift"
        assert row["shift_offsets"] >= MIN_OFFSETS
    assert set(run["by_direction"]) == {"BULLISH", "BEARISH"}
    assert any("circular-shift" in n for n in run["notes"])
