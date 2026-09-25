"""
Cross-session labelling tests.

Exchange EOD history has one bar per session, so the intraday labeller scored
n=0 at every horizon and the EOD label compared a bar with itself. These pin the
daily path: horizons that count trading sessions, a weekday-matched null, and an
engine that picks the mode from the data rather than from a flag someone can
forget.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.benchmark import build_daily_null_labels, sample_null_sessions  # noqa: E402
from signals.base import Direction, Signal, SignalResult  # noqa: E402
from signals.registry import register_signal  # noqa: E402
from backtest.labels import (  # noqa: E402
    HORIZONS_DAYS, compute_daily_forward_returns, daily_horizon_keys,
)

SYM = "NIFTY"


# min_history=0 on purpose: `replay_session` builds its history window WITHIN a
# session, so one-bar-per-session data never accumulates any. Cross-session
# history windows are an open roadmap item; a daily signal needing prior bars
# currently has to receive them through `extras`, as the VRP signal does.
@register_signal("intraday_probe", version=1, min_history=0)
def _intraday_probe(ctx):
    """Fires on every bar, so mode selection is what is being tested."""
    return SignalResult.fire(Signal(
        signal_id="intraday_probe", version=1, ts=ctx.ts, symbol=ctx.symbol,
        direction=Direction.BULLISH, strength=0.5,
    ))


def weekdays(n: int, start="2026-01-05") -> list[str]:
    """`n` consecutive weekdays — a stand-in for a session calendar."""
    d = datetime.strptime(start, "%Y-%m-%d").date()
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ── Horizons count sessions, not calendar days ───────────────────────────────

def test_horizons_are_trading_sessions_not_calendar_days():
    """
    A holiday or long weekend must not silently shorten a horizon. +1d means
    the next session we actually hold, whatever the gap in dates.
    """
    dates = ["2026-01-05", "2026-01-06", "2026-01-20"]   # 2-week gap
    closes = [100.0, 110.0, 120.0]
    lab = compute_daily_forward_returns(0, dates, closes, SYM, horizons_days=(1, 2))
    assert lab.returns_bps["1d"] == 1000.0      # 100 -> 110
    assert lab.returns_bps["2d"] == 2000.0      # 100 -> 120, despite the gap


def test_returns_at_each_horizon():
    dates = weekdays(30)
    closes = [24000.0 + i * 100 for i in range(30)]
    lab = compute_daily_forward_returns(0, dates, closes, SYM)

    assert set(lab.returns_bps) == set(daily_horizon_keys())
    for key, sessions in zip(daily_horizon_keys(), HORIZONS_DAYS):
        expected = (closes[sessions] / closes[0] - 1) * 10_000
        assert abs(lab.returns_bps[key] - expected) < 0.01


def test_horizon_past_the_end_is_none_not_truncated():
    """A 20-session label built from 4 sessions is a different statistic."""
    dates = weekdays(25)
    closes = [24000.0 + i * 10 for i in range(25)]
    lab = compute_daily_forward_returns(21, dates, closes, SYM)
    assert lab.returns_bps["1d"] is not None
    assert lab.returns_bps["5d"] is None
    assert lab.returns_bps["20d"] is None


def test_direction_flips_the_sign():
    dates = weekdays(10)
    closes = [24000.0 + i * 100 for i in range(10)]
    long_lab = compute_daily_forward_returns(0, dates, closes, SYM,
                                             horizons_days=(1,), direction=1)
    short_lab = compute_daily_forward_returns(0, dates, closes, SYM,
                                              horizons_days=(1,), direction=-1)
    assert long_lab.returns_bps["1d"] == -short_lab.returns_bps["1d"]


def test_mae_mfe_bracket_the_realised_return():
    dates = weekdays(10)
    closes = [24000.0, 23500.0, 24800.0, 24200.0] + [24200.0] * 6
    lab = compute_daily_forward_returns(0, dates, closes, SYM, horizons_days=(3,))
    assert lab.mae_bps["3d"] < 0 < lab.mfe_bps["3d"]
    assert lab.mae_bps["3d"] <= lab.returns_bps["3d"] <= lab.mfe_bps["3d"]


def test_uses_only_sessions_after_the_entry():
    """No look-ahead: the entry bar's own close is the base, never a label."""
    dates = weekdays(10)
    closes = [100.0] * 5 + [200.0] * 5
    # entry at index 4 (last 100) -> +1d is the first 200
    lab = compute_daily_forward_returns(4, dates, closes, SYM, horizons_days=(1,))
    assert lab.returns_bps["1d"] == 10000.0
    assert lab.spot_at_signal == 100.0


# ── Weekday-matched null ─────────────────────────────────────────────────────

def test_null_matches_weekday_when_there_are_enough_sessions():
    """
    The weekly expiry cycle sits on a fixed weekday, so a signal that fires
    mostly on expiry days must be compared against random entries on the same
    weekday rather than against the week as a whole.
    """
    dates = weekdays(200)
    tuesday_indices = [i for i, d in enumerate(dates)
                       if datetime.strptime(d, "%Y-%m-%d").weekday() == 1]

    picks = sample_null_sessions(tuesday_indices[:5], dates,
                                 samples_per_signal=20, seed=7)
    assert picks
    assert all(datetime.strptime(dates[p], "%Y-%m-%d").weekday() == 1
               for p in picks)


def test_null_falls_back_when_a_weekday_is_too_thin():
    """A match drawn from three repeated days is worse than no match."""
    dates = weekdays(6)                      # ~1-2 of each weekday
    picks = sample_null_sessions([0], dates, samples_per_signal=10, seed=1)
    assert len(picks) == 10
    assert len({dates[p] for p in picks}) > 1   # drew from the whole range


def test_null_is_reproducible_from_the_seed():
    dates = weekdays(100)
    a = sample_null_sessions([0, 5], dates, samples_per_signal=5, seed=42)
    b = sample_null_sessions([0, 5], dates, samples_per_signal=5, seed=42)
    c = sample_null_sessions([0, 5], dates, samples_per_signal=5, seed=43)
    assert a == b and a != c


def test_null_labels_mirror_the_signals_direction():
    dates = weekdays(40)
    closes = [24000.0 + i * 50 for i in range(40)]
    picks = [0, 1, 2]
    labels = build_daily_null_labels(picks, dates, closes, SYM,
                                     directions=[-1, -1, -1])
    # Short in a rising series must label negative.
    assert all(lab.returns_bps["1d"] < 0 for lab in labels)


def test_empty_inputs_are_handled():
    assert sample_null_sessions([], weekdays(10)) == []
    assert sample_null_sessions([0], []) == []
    assert build_daily_null_labels([], weekdays(5), [1.0] * 5, SYM) == []


# ── Mode selection in the engine ─────────────────────────────────────────────

def _one_bar_per_session_db(n_sessions=30):
    """A store with exactly one snapshot per session — the EOD shape."""
    from market_hours import IST, SessionPhase
    from recorder.models import ChainRow, ChainSnapshot
    from recorder.store import init_db, write_snapshot
    from signals.store import init_db as init_sig

    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(db)
    init_sig(db)

    for i, d in enumerate(weekdays(n_sessions)):
        spot = 24000.0 + i * 40
        rows = []
        for k in range(23600, 24500, 100):
            for opt in ("CE", "PE"):
                skew = (max(0.0, k - spot) if opt == "CE" else max(0.0, spot - k)) / 10
                rows.append(ChainRow(
                    strike=float(k), option_type=opt,
                    oi=1000.0 + skew * 20, ltp=120.0, bid=0.0, ask=0.0,
                    volume=800.0))
        write_snapshot(ChainSnapshot(
            ts=f"{d}T15:30:00+05:30", session_date=d,
            session_phase=SessionPhase.END_OF_DAY, symbol=SYM,
            expiry_date="29-10-2026", expiry_epoch=1793000000,
            spot=spot, rows=tuple(rows)), db)
    return db


def test_engine_picks_daily_mode_from_the_data():
    """
    Detected, not configured: a caller must not be able to score daily bars on
    intraday horizons by forgetting a flag.
    """
    from backtest.engine import run_backtest
    import signals.library  # noqa: F401

    db = _one_bar_per_session_db()
    try:
        # A DIRECTIONAL signal: gex_regime emits SHORT_VOL/LONG_VOL and is
        # therefore scored on vol outcomes, not on daily direction.
        run = run_backtest("intraday_probe", SYM, db_path=db)
        out = run.to_dict()
        assert out["label_mode"] == "daily"
        assert out["unit"] == "bps"
        assert set(out["horizons"]) == {"1d", "5d", "20d"}
        assert any("weekday" in n for n in out["notes"])
    finally:
        os.unlink(db)


def _minute_bar_db(n_bars=40):
    """A store with many bars per session — the recorder shape."""
    from market_hours import IST, SessionPhase
    from recorder.models import ChainRow, ChainSnapshot
    from recorder.store import init_db, write_snapshot
    from signals.store import init_db as init_sig

    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(db)
    init_sig(db)

    for d in ("2026-09-21", "2026-09-22"):
        start = datetime.fromisoformat(f"{d}T09:15:00").replace(tzinfo=IST)
        for i in range(n_bars):
            ts = (start + timedelta(minutes=i)).isoformat()
            write_snapshot(ChainSnapshot(
                ts=ts, session_date=d, session_phase=SessionPhase.CONTINUOUS,
                symbol=SYM, expiry_date="29-10-2026", expiry_epoch=1793000000,
                spot=24000.0 + i,
                rows=(ChainRow(strike=24000.0, option_type="CE", ltp=100.0,
                               bid=99.5, ask=100.5, oi=1000.0, volume=500.0),)),
                db)
    return db


def test_intraday_mode_still_selected_for_minute_bars():
    """
    Self-contained on purpose. Importing a fixture from another test module
    re-executes its @register_signal decorators under a second module identity
    (pytest imports test files without a package prefix), which trips the
    registry's duplicate guard only when the whole suite runs.
    """
    from backtest.engine import run_backtest
    import signals.library  # noqa: F401

    db = _minute_bar_db()
    try:
        run = run_backtest("intraday_probe", SYM, db_path=db)
        out = run.to_dict()
        assert out["label_mode"] == "intraday"
        assert "5m" in out["horizons"]
        assert "1d" not in out["horizons"]
    finally:
        os.unlink(db)


def test_explicit_mode_overrides_detection():
    from backtest.engine import run_backtest
    import signals.library  # noqa: F401

    db = _one_bar_per_session_db()
    try:
        out = run_backtest("intraday_probe", SYM, db_path=db,
                           label_mode="intraday").to_dict()
        assert out["label_mode"] == "intraday"
    finally:
        os.unlink(db)
