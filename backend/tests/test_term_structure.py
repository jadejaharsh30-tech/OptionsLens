"""
Term structure and skew tests (roadmap item 33).

The properties that carry the weight are the ones that would fail silently:

  - **The two curve legs are joined on DATE.** The 60-day leg blanks whenever
    the far month did not trade, and it blanks far more often than the 30-day
    leg. Zipping the two would pair today's front with last week's back and
    report the difference as a slope.
  - **A percentile is not a threshold.** An index curve is in contango nearly
    all the time, so its 20th percentile is still a normal curve. Firing
    LONG_VOL there would trade "slightly less steep than usual" as though it
    were stress, which is why the percentile band and the absolute guard are
    both required.
  - **The skew series is one reading per session, taken at the close.** A
    10:15 risk reversal ranked against closing readings measures the time of
    day.
  - **No look-ahead**, through the same strictly-`<` slice every ranked signal
    uses.
"""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import RISK_FREE_RATE  # noqa: E402
from iv_engine import black76_price  # noqa: E402
from market_hours import SessionPhase  # noqa: E402
from recorder.models import ChainRow, ChainSnapshot  # noqa: E402
from signals.base import Direction, SignalContext  # noqa: E402
from signals.registry import get_signal  # noqa: E402
from signals.skew_signal import EXTRAS_KEY as SKEW_KEY  # noqa: E402
from signals.skew_signal import SIGNAL_ID as SKEW_ID  # noqa: E402
from signals.term_structure_signal import EXTRAS_KEY as TS_KEY  # noqa: E402
from signals.term_structure_signal import SIGNAL_ID as TS_ID  # noqa: E402
from skew import (  # noqa: E402
    build_skew_series, load_skew_history, rr25_from_snapshot, skew_percentile,
)
from term_structure import (  # noqa: E402
    build_slope_series, load_term_structure_history, slope_percentile, summarise,
)

SYM = "NIFTY"
SPOT = 24000.0
TENOR_DAYS = 30.0


# ── Curve joining ─────────────────────────────────────────────────────────────

def test_slope_join_is_on_date_not_position():
    """
    The far leg has gaps the near leg does not. A positional pair would report
    a slope between two different days and never look wrong.
    """
    near = [{"date": "2026-01-05", "iv": 0.20},
            {"date": "2026-01-06", "iv": 0.21},
            {"date": "2026-01-07", "iv": 0.22}]
    far = [{"date": "2026-01-05", "iv": 0.22},
           {"date": "2026-01-07", "iv": 0.23}]          # 6th missing

    series = build_slope_series(near, far)
    assert [r["date"] for r in series] == ["2026-01-05", "2026-01-07"]
    assert series[0]["slope"] == 2.0                    # (0.22-0.20)*100
    assert series[1]["slope"] == 1.0                    # (0.23-0.22), not 0.23-0.21


def test_slope_is_reported_in_vol_points():
    series = build_slope_series([{"date": "d", "iv": 0.18}],
                                [{"date": "d", "iv": 0.195}])
    assert series[0]["slope"] == 1.5


def test_inversion_is_a_negative_slope():
    series = build_slope_series([{"date": "d", "iv": 0.30}],
                                [{"date": "d", "iv": 0.24}])
    assert series[0]["slope"] == -6.0


def test_slope_join_drops_unusable_rows():
    near = [{"date": "d1", "iv": None}, {"date": None, "iv": 0.2},
            {"date": "d2", "iv": 0.2}]
    far = [{"date": "d1", "iv": 0.21}, {"date": "d2", "iv": None}]
    assert build_slope_series(near, far) == []


def test_slope_series_is_sorted_ascending():
    near = [{"date": d, "iv": 0.2} for d in ("2026-01-07", "2026-01-05", "2026-01-06")]
    far = [{"date": d, "iv": 0.22} for d in ("2026-01-05", "2026-01-06", "2026-01-07")]
    got = [r["date"] for r in build_slope_series(near, far)]
    assert got == sorted(got)


def test_summarise_reports_inversion_share():
    series = [{"date": f"2026-01-{i:02d}", "near_iv": 0.2, "far_iv": 0.21,
               "slope": s}
              for i, s in enumerate([1.0, 1.0, -1.0, 1.0], start=1)]
    info = summarise(series)
    assert info["observations"] == 4
    assert info["pct_inverted"] == 25.0


def test_summarise_of_an_empty_series_explains_the_far_leg():
    info = summarise([])
    assert info["observations"] == 0
    assert "60-day" in info["note"]


def test_slope_percentile_needs_enough_observations():
    thin = [{"slope": float(i)} for i in range(10)]
    assert slope_percentile(thin, 5.0, min_observations=60) is None
    enough = [{"slope": float(i)} for i in range(100)]
    assert slope_percentile(enough, 50.0, min_observations=60) is not None


# ── The term-structure signal ─────────────────────────────────────────────────

def make_snapshot(session_date: str, rows=None,
                  phase: SessionPhase = SessionPhase.END_OF_DAY) -> ChainSnapshot:
    return ChainSnapshot(
        ts=f"{session_date}T15:30:00+05:30", session_date=session_date,
        session_phase=phase, symbol=SYM,
        expiry_date=_expiry_in(TENOR_DAYS), expiry_epoch=0, spot=SPOT,
        rows=rows if rows is not None else
        (ChainRow(strike=SPOT, option_type="CE", ltp=100.0),),
    )


def slope_series_ending(session_date: str, today_slope: float, n: int = 120,
                        base=lambda i: (i % 21) * 0.1 + 0.5) -> list[dict]:
    """`n` prior curve readings plus one for `session_date`."""
    d = dt.date.fromisoformat(session_date)
    out = []
    for i in range(n, 0, -1):
        day = d - dt.timedelta(days=i)
        out.append({"date": day.isoformat(), "near_iv": 0.18, "far_iv": 0.19,
                    "slope": round(base(i), 4)})
    out.append({"date": session_date, "near_iv": 0.18,
                "far_iv": 0.18 + today_slope / 100.0, "slope": today_slope})
    return out


def evaluate_ts(session_date: str, series: list[dict], params=None):
    spec = get_signal(TS_ID)
    ctx = SignalContext(snapshot=make_snapshot(session_date), history=(),
                        params=params or {}, extras={TS_KEY: series})
    return spec.evaluate(ctx, params)


def test_steep_contango_fires_short_vol():
    d = "2026-06-15"
    res = evaluate_ts(d, slope_series_ending(d, today_slope=8.0))
    assert res.fired
    assert res.signal.direction is Direction.SHORT_VOL
    assert res.signal.features["slope_percentile"] >= 80
    assert res.signal.features["inverted"] is False


def test_inversion_fires_long_vol():
    d = "2026-06-15"
    res = evaluate_ts(d, slope_series_ending(d, today_slope=-3.0))
    assert res.fired
    assert res.signal.direction is Direction.LONG_VOL
    assert res.signal.features["inverted"] is True


def test_a_low_percentile_in_persistent_contango_is_not_inversion():
    """
    The guard that stops this trading "flatter than usual" as though it were
    stress. Every prior reading is a steep normal curve; today's is the
    flattest of them but still comfortably positive.
    """
    d = "2026-06-15"
    series = slope_series_ending(d, today_slope=1.2,
                                 base=lambda i: 2.0 + (i % 17) * 0.1)
    res = evaluate_ts(d, series)
    assert not res.fired
    assert res.features["slope_percentile"] <= 20
    assert "still in contango" in res.reason


def test_a_flat_curves_top_decile_is_refused():
    """A percentile can be extreme while the slope is inside measurement noise."""
    d = "2026-06-15"
    series = slope_series_ending(d, today_slope=0.05, base=lambda i: 0.0)
    res = evaluate_ts(d, series)
    assert not res.fired
    assert "still flat" in res.reason or "flat" in res.reason


def test_mid_curve_does_not_fire():
    d = "2026-06-15"
    series = slope_series_ending(d, today_slope=2.0,
                                 base=lambda i: (i % 41) * 0.1)
    res = evaluate_ts(d, series)
    assert not res.fired
    assert "percentile" in res.reason


def test_missing_series_skips_with_an_actionable_reason():
    res = evaluate_ts("2026-06-15", [])
    assert not res.fired
    assert "extras" in res.reason


def test_a_date_with_no_far_leg_skips():
    d = "2026-06-15"
    series = slope_series_ending(d, today_slope=5.0)[:-1]   # today's row removed
    res = evaluate_ts(d, series)
    assert not res.fired
    assert "far leg" in res.reason


def test_thin_history_skips_with_a_countable_reason():
    d = "2026-06-15"
    res = evaluate_ts(d, slope_series_ending(d, today_slope=8.0, n=10))
    assert not res.fired
    assert "10 prior" in res.reason


def test_todays_reading_is_excluded_from_its_own_percentile():
    """
    A duplicated extreme would drag the percentile toward the middle if today
    were counted. The reading must be ranked against strictly prior dates.
    """
    d = "2026-06-15"
    series = slope_series_ending(d, today_slope=99.0)
    res = evaluate_ts(d, series)
    assert res.fired
    assert res.signal.features["slope_percentile"] == 100.0
    assert res.signal.features["history_size"] == 120


# ── Risk reversal ─────────────────────────────────────────────────────────────

def _expiry_in(days: float) -> str:
    """A Fyers-format expiry `days` ahead of now, so T never goes stale."""
    return (dt.date.today() + dt.timedelta(days=int(days))).strftime("%d-%m-%Y")


def skewed_chain(skew_slope: float, base_iv: float = 0.18,
                 forward: float = SPOT) -> tuple[ChainRow, ...]:
    """
    A chain priced from a linear smile, so its risk reversal is known by design.

    Both sides are priced off the same forward, so put-call parity holds exactly
    and `implied_forward_for_chain` recovers it — the IVs the features layer
    solves back out are the ones put in. A positive `skew_slope` lifts the put
    wing, which is the normal index shape and a NEGATIVE risk reversal.
    """
    T = TENOR_DAYS / 365.0
    rows = []
    for i in range(-10, 11):
        K = forward + i * 200.0
        iv = base_iv + skew_slope * (forward - K) / forward
        for opt in ("CE", "PE"):
            price = black76_price(forward, K, T, RISK_FREE_RATE, iv, opt)
            rows.append(ChainRow(strike=K, option_type=opt,
                                 ltp=round(price, 2), oi=1000.0, volume=100.0))
    return tuple(rows)


def test_a_normal_index_smile_gives_a_negative_risk_reversal():
    snap = make_snapshot("2026-06-15", rows=skewed_chain(0.6))
    rr = rr25_from_snapshot(snap)
    assert rr is not None
    assert rr < 0


def test_a_flat_smile_gives_a_risk_reversal_near_zero():
    snap = make_snapshot("2026-06-15", rows=skewed_chain(0.0))
    rr = rr25_from_snapshot(snap)
    assert rr is not None
    assert abs(rr) < 0.5


def test_a_steeper_put_wing_is_a_more_negative_risk_reversal():
    mild = rr25_from_snapshot(make_snapshot("2026-06-15", rows=skewed_chain(0.3)))
    steep = rr25_from_snapshot(make_snapshot("2026-06-15", rows=skewed_chain(0.9)))
    assert steep < mild


def test_an_unpriceable_chain_yields_no_reading():
    """None, not a fabricated zero — the wings simply did not trade."""
    rows = (ChainRow(strike=SPOT, option_type="CE", ltp=100.0),)
    assert rr25_from_snapshot(make_snapshot("2026-06-15", rows=rows)) is None


def test_skew_series_takes_one_reading_per_session_from_its_last_bar():
    """
    Two bars on one date, different smiles. The closing bar is the one that must
    survive, because every other daily series here is stamped at the close.
    """
    early = ChainSnapshot(
        ts="2026-06-15T10:15:00+05:30", session_date="2026-06-15",
        session_phase=SessionPhase.CONTINUOUS, symbol=SYM,
        expiry_date=_expiry_in(TENOR_DAYS), expiry_epoch=0, spot=SPOT,
        rows=skewed_chain(0.2))
    close = ChainSnapshot(
        ts="2026-06-15T15:30:00+05:30", session_date="2026-06-15",
        session_phase=SessionPhase.END_OF_DAY, symbol=SYM,
        expiry_date=_expiry_in(TENOR_DAYS), expiry_epoch=0, spot=SPOT,
        rows=skewed_chain(1.0))

    series = build_skew_series([early, close])
    assert len(series) == 1
    assert series[0]["date"] == "2026-06-15"
    # Stored rounded to 4dp; the point is that it is the CLOSE's reading.
    assert abs(series[0]["rr_25d"] - rr25_from_snapshot(close)) < 1e-4
    assert abs(series[0]["rr_25d"] - rr25_from_snapshot(early)) > 1.0


def test_skew_series_omits_rather_than_carries_a_dead_session():
    """A repeated stale value would be counted as a fresh observation."""
    good = make_snapshot("2026-06-15", rows=skewed_chain(0.6))
    dead = make_snapshot("2026-06-16",
                         rows=(ChainRow(strike=SPOT, option_type="CE", ltp=100.0),))
    series = build_skew_series([good, dead])
    assert [r["date"] for r in series] == ["2026-06-15"]


def test_skew_percentile_needs_enough_observations():
    thin = [{"rr_25d": float(i)} for i in range(10)]
    assert skew_percentile(thin, -2.0, min_observations=60) is None


# ── load_skew_history reads one bar per session ───────────────────────────────

def test_load_skew_history_reads_the_closing_bar_of_each_session():
    from recorder.store import init_db, last_snapshot_of_session, write_snapshot

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "eod.db")
        init_db(db)
        for day, slope in (("2026-06-15", 0.3), ("2026-06-16", 0.9)):
            write_snapshot(ChainSnapshot(
                ts=f"{day}T10:15:00+05:30", session_date=day,
                session_phase=SessionPhase.CONTINUOUS, symbol=SYM,
                expiry_date=_expiry_in(TENOR_DAYS), expiry_epoch=0, spot=SPOT,
                rows=skewed_chain(0.0)), db)
            write_snapshot(make_snapshot(day, rows=skewed_chain(slope)), db)

        last = last_snapshot_of_session(SYM, "2026-06-15", db)
        assert last is not None and last.ts.endswith("15:30:00+05:30")
        assert last_snapshot_of_session(SYM, "2026-01-01", db) is None

        series = load_skew_history(db, SYM)
        assert [r["date"] for r in series] == ["2026-06-15", "2026-06-16"]
        # The closing bars were the skewed ones; the 10:15 bars were flat.
        assert all(r["rr_25d"] < -0.5 for r in series)
        assert series[1]["rr_25d"] < series[0]["rr_25d"]


# ── The skew signal ───────────────────────────────────────────────────────────

def skew_series_ending(session_date: str, n: int = 120,
                       base=lambda i: -2.0 - (i % 21) * 0.1) -> list[dict]:
    """`n` prior risk-reversal readings, strictly before `session_date`."""
    d = dt.date.fromisoformat(session_date)
    return [{"date": (d - dt.timedelta(days=i)).isoformat(),
             "rr_25d": round(base(i), 4)}
            for i in range(n, 0, -1)]


def evaluate_skew(session_date: str, series: list[dict], skew_slope: float,
                  params=None, phase: SessionPhase = SessionPhase.END_OF_DAY):
    spec = get_signal(SKEW_ID)
    snap = make_snapshot(session_date, rows=skewed_chain(skew_slope), phase=phase)
    ctx = SignalContext(snapshot=snap, history=(), params=params or {},
                        extras={SKEW_KEY: series})
    return spec.evaluate(ctx, params)


def test_an_unusually_bid_put_wing_is_bullish_in_contrarian_mode():
    d = "2026-06-15"
    # History hovers near -2; today's wing is far steeper than any of it.
    res = evaluate_skew(d, skew_series_ending(d), skew_slope=1.2)
    assert res.fired
    assert res.signal.direction is Direction.BULLISH
    assert res.signal.features["rr_percentile"] <= 10


def test_the_same_reading_is_bearish_in_momentum_mode():
    """The contested direction is a parameter, and it is the only difference."""
    d = "2026-06-15"
    contra = evaluate_skew(d, skew_series_ending(d), skew_slope=1.2)
    momentum = evaluate_skew(d, skew_series_ending(d), skew_slope=1.2,
                             params={"mode": "momentum"})
    assert contra.fired and momentum.fired
    assert contra.signal.direction is Direction.BULLISH
    assert momentum.signal.direction is Direction.BEARISH
    assert contra.signal.features["rr_25d"] == momentum.signal.features["rr_25d"]


def test_an_unusually_bid_call_wing_is_bearish_in_contrarian_mode():
    d = "2026-06-15"
    # History is a steep put bid; today's chain is flat, so calls are rich
    # relative to everything that came before.
    res = evaluate_skew(d, skew_series_ending(d, base=lambda i: -6.0 - (i % 11) * 0.1),
                        skew_slope=0.0)
    assert res.fired
    assert res.signal.direction is Direction.BEARISH
    assert res.signal.features["rr_percentile"] >= 90


def test_the_middle_of_the_distribution_does_not_fire():
    d = "2026-06-15"
    series = skew_series_ending(d, base=lambda i: -3.0 + ((i % 41) - 20) * 0.3)
    res = evaluate_skew(d, series, skew_slope=0.6)
    assert not res.fired
    assert "percentile" in res.reason


def test_an_intraday_bar_is_refused_by_default():
    """Ranked against closing readings it would measure the time of day."""
    d = "2026-06-15"
    res = evaluate_skew(d, skew_series_ending(d), skew_slope=1.2,
                        phase=SessionPhase.CONTINUOUS)
    assert not res.fired
    assert "end-of-day" in res.reason


def test_an_intraday_bar_can_be_allowed_deliberately():
    d = "2026-06-15"
    res = evaluate_skew(d, skew_series_ending(d), skew_slope=1.2,
                        params={"require_eod_bar": False},
                        phase=SessionPhase.CONTINUOUS)
    assert res.fired


def test_a_chain_with_no_wings_skips_rather_than_defaulting():
    d = "2026-06-15"
    spec = get_signal(SKEW_ID)
    snap = make_snapshot(d, rows=(ChainRow(strike=SPOT, option_type="CE", ltp=100.0),))
    ctx = SignalContext(snapshot=snap, history=(), params={},
                        extras={SKEW_KEY: skew_series_ending(d)})
    res = spec.evaluate(ctx, {})
    assert not res.fired
    assert "25-delta pair" in res.reason


def test_missing_skew_series_skips_with_an_actionable_reason():
    res = evaluate_skew("2026-06-15", [], skew_slope=0.6)
    assert not res.fired
    assert "extras" in res.reason


def test_an_unknown_mode_is_refused_rather_than_guessed():
    d = "2026-06-15"
    res = evaluate_skew(d, skew_series_ending(d), skew_slope=1.2,
                        params={"mode": "vibes"})
    assert not res.fired
    assert "unknown mode" in res.reason


def test_skew_history_excludes_the_current_session():
    """
    The series may already carry today's row — `load_skew_history` builds it
    from the same store the replay walks. Ranking against it would include the
    observation in its own distribution.
    """
    d = "2026-06-15"
    series = skew_series_ending(d) + [{"date": d, "rr_25d": -99.0}]
    res = evaluate_skew(d, series, skew_slope=1.2)
    assert res.fired
    assert res.signal.features["history_size"] == 120


# ── End to end through the backtester ─────────────────────────────────────────

def weekdays(n: int, start="2026-01-05") -> list[str]:
    d = dt.date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def _eod_store(tmp: str, dates: list[str], skew_slope=lambda i: 0.6) -> str:
    """One EOD bar per session, priced from a smile that can vary by day."""
    from recorder.store import init_db, write_snapshot

    db = os.path.join(tmp, "eod.db")
    init_db(db)
    for i, d in enumerate(dates):
        write_snapshot(ChainSnapshot(
            ts=f"{d}T15:30:00+05:30", session_date=d,
            session_phase=SessionPhase.END_OF_DAY, symbol=SYM,
            expiry_date=_expiry_in(TENOR_DAYS), expiry_epoch=0,
            spot=SPOT * (1.0 + 0.001 * ((i % 7) - 3)),
            rows=skewed_chain(skew_slope(i))), db)
    return db


def test_term_structure_runs_end_to_end_and_is_scored_on_vol():
    """
    The plumbing check the UI depends on: a vol-family signal must come back in
    VOL POINTS, not bps. Mixing the two units in one table is how a volatility
    result gets read as a directional one.
    """
    from backtest.engine import run_backtest

    dates = weekdays(140)
    with tempfile.TemporaryDirectory() as tmp:
        db = _eod_store(tmp, dates)
        # A curve that steepens over the window, so the late sessions sit in
        # the top decile of everything before them.
        series = [{"date": d, "near_iv": 0.18, "far_iv": 0.18 + i * 0.0005,
                   "slope": round(i * 0.05, 4)}
                  for i, d in enumerate(dates)]
        run = run_backtest(
            "term_structure", SYM, db_path=db, persist_evaluations=False,
            extras={TS_KEY: series,
                    "iv_series": [{"date": d, "iv": 0.18} for d in dates]},
        ).to_dict()

    assert run["signals_fired"] > 0
    assert run["label_mode"] == "vol"
    assert run["unit"] == "vol_points"


def test_skew_runs_end_to_end_and_is_scored_on_direction():
    """A BULLISH/BEARISH signal gets daily horizons and bps, not vol points."""
    from backtest.engine import run_backtest

    dates = weekdays(140)
    with tempfile.TemporaryDirectory() as tmp:
        # A put wing that steepens steadily, so the last sessions are in the
        # bottom decile of the history that precedes them.
        db = _eod_store(tmp, dates, skew_slope=lambda i: 0.2 + i * 0.006)
        series = load_skew_history(db, SYM)
        assert len(series) == len(dates)          # every session priced a wing

        run = run_backtest("skew_rr25", SYM, db_path=db,
                           persist_evaluations=False,
                           extras={SKEW_KEY: series}).to_dict()

    assert run["signals_fired"] > 0
    assert run["label_mode"] == "daily"
    assert run["unit"] == "bps"


def test_a_signal_without_its_series_fires_nothing_and_says_so():
    """
    The failure mode this whole extras plumbing exists to prevent: a registered
    signal that skips every bar must report that, not return an empty table.
    """
    from backtest.engine import run_backtest

    dates = weekdays(30)
    with tempfile.TemporaryDirectory() as tmp:
        db = _eod_store(tmp, dates)
        run = run_backtest("term_structure", SYM, db_path=db,
                           persist_evaluations=False).to_dict()

    assert run["signals_fired"] == 0
    assert any("No signals fired" in n for n in run["notes"])


# ── The loaders reach the database ────────────────────────────────────────────

def test_load_term_structure_history_joins_two_tenors_from_one_table():
    from snapshot_store import init_db, write_atm_iv

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "app.db")
        init_db(db)

        # Two expiries per date bracketing 30 days, two more bracketing 60.
        base = dt.date(2026, 3, 2)
        for i in range(5):
            d = (base + dt.timedelta(days=i)).isoformat()
            for offset, iv in ((20, 0.18), (40, 0.19), (75, 0.21)):
                expiry = (base + dt.timedelta(days=i + offset)).strftime("%d-%m-%Y")
                write_atm_iv(db, d, SYM, expiry, iv)

        series = load_term_structure_history(db, SYM)
        assert len(series) == 5
        assert all(r["slope"] > 0 for r in series)      # rising curve
        assert [r["date"] for r in series] == sorted(r["date"] for r in series)


def test_term_structure_coverage_reports_a_missing_far_leg():
    """
    The failure the joined series cannot show: a healthy-looking 30-day history
    whose 60-day leg only exists on a minority of dates.
    """
    from snapshot_store import init_db, write_atm_iv
    from term_structure import coverage

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "app.db")
        init_db(db)

        base = dt.date(2026, 3, 2)
        for i in range(10):
            d = (base + dt.timedelta(days=i)).isoformat()
            offsets = [(20, 0.18), (40, 0.19)]
            if i < 3:                                   # far month only sometimes
                offsets.append((75, 0.21))
            for offset, iv in offsets:
                expiry = (base + dt.timedelta(days=i + offset)).strftime("%d-%m-%Y")
                write_atm_iv(db, d, SYM, expiry, iv)

        cov = coverage(db, SYM)
        assert cov["near_dates"] == 10
        assert cov["paired"] == 3
        assert cov["far_leg_coverage_pct"] == 30.0
