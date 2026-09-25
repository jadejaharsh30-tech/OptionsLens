"""
Variance risk premium tests (roadmap item 30) and the RV dating fix.

Two properties carry the weight:

  - **Dates are real.** The old RV series reconstructed dates by counting
    weekdays backwards from today, so every exchange holiday shifted it against
    the IV series. VRP is IV minus RV, so that offset landed in the signal, not
    merely in a chart.
  - **No look-ahead.** Ranking today's spread against a history that includes
    today, or any later date, inflates the result invisibly. The slicing is one
    tested function precisely so it cannot be re-derived wrongly per caller.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from realized_vol import compute_realized_vol, rv_series_from_dated_closes  # noqa: E402
from signals.base import Direction, SignalContext  # noqa: E402
from signals.registry import get_signal  # noqa: E402
from signals.vrp_signal import EXTRAS_KEY, SIGNAL_ID  # noqa: E402
from vrp import (  # noqa: E402
    build_vrp_series, load_vrp_history, observations_before, summarise,
    vrp_percentile,
)

SYM = "NIFTY"


def dates_with_holiday() -> list[str]:
    """January weekdays with the 14th missing — an exchange holiday."""
    days = [d for d in range(1, 31)]
    out = []
    import datetime as dt
    for d in days:
        date = dt.date(2026, 1, d)
        if date.weekday() >= 5 or d == 14:
            continue
        out.append(date.isoformat())
    return out


# ── RV dating ─────────────────────────────────────────────────────────────────

def test_rv_dates_survive_a_holiday_unshifted():
    """The bug this replaces: a holiday used to slide every later date."""
    ds = dates_with_holiday()
    rows = [{"date": d, "spot": 24000.0 + i * 25} for i, d in enumerate(ds)]
    series = rv_series_from_dated_closes(rows, window=5)

    got = [r["date"] for r in series]
    assert "2026-01-14" not in got                 # the holiday is not invented
    assert got == sorted(got)
    assert got[-1] == ds[-1]                       # last RV sits on the last close
    assert set(got).issubset(set(ds))              # never a non-session date


def test_rv_is_dated_at_the_end_of_its_window():
    """RV on date D must be knowable at D's close, so it pairs with D's IV."""
    rows = [{"date": f"2026-03-{d:02d}", "spot": 100.0 + d} for d in range(1, 21)]
    series = rv_series_from_dated_closes(rows, window=5)
    assert series[0]["date"] == rows[5]["date"]    # first full window ends here


def test_rv_series_rejects_short_history_and_bad_rows():
    rows = [{"date": "2026-03-01", "spot": 100.0}]
    assert rv_series_from_dated_closes(rows, window=20) == []

    dirty = [{"date": "2026-03-01", "spot": 0.0},
             {"date": None, "spot": 100.0},
             {"date": "2026-03-02", "spot": None}]
    assert rv_series_from_dated_closes(dirty, window=2) == []


def test_rv_series_sorts_unordered_input():
    rows = [{"date": f"2026-03-{d:02d}", "spot": 100.0 + d} for d in (5, 1, 3, 2, 4, 6, 7)]
    series = rv_series_from_dated_closes(rows, window=3)
    assert [r["date"] for r in series] == sorted(r["date"] for r in series)


def test_rv_matches_the_scalar_estimator():
    rows = [{"date": f"2026-03-{d:02d}", "spot": 100.0 * (1.001 ** d)}
            for d in range(1, 12)]
    series = rv_series_from_dated_closes(rows, window=10)
    direct = compute_realized_vol([r["spot"] for r in rows], window=10)
    assert abs(series[-1]["rv"] - direct) < 1e-12


# ── Joining IV and RV ─────────────────────────────────────────────────────────

def test_join_is_on_date_not_position():
    """
    Positional pairing is exactly what the old code did wrong. A gap on one side
    must produce a missing observation, not a silent one-day offset.
    """
    iv = [{"date": "2026-01-05", "iv": 0.20},
          {"date": "2026-01-06", "iv": 0.21},
          {"date": "2026-01-07", "iv": 0.22}]
    rv = [{"date": "2026-01-05", "rv": 0.15},
          {"date": "2026-01-07", "rv": 0.17}]          # 6th missing

    series = build_vrp_series(iv, rv)
    assert [r["date"] for r in series] == ["2026-01-05", "2026-01-07"]
    assert series[0]["vrp"] == 5.0                     # (0.20-0.15)*100
    assert series[1]["vrp"] == 5.0                     # (0.22-0.17), not 0.21-0.17


def test_join_reports_vol_points():
    series = build_vrp_series([{"date": "d", "iv": 0.18}], [{"date": "d", "rv": 0.12}])
    assert series[0]["vrp"] == 6.0


def test_join_drops_unusable_rows():
    iv = [{"date": "d1", "iv": None}, {"date": None, "iv": 0.2},
          {"date": "d2", "iv": 0.2}]
    rv = [{"date": "d1", "rv": 0.1}, {"date": "d2", "rv": None}]
    assert build_vrp_series(iv, rv) == []


# ── Look-ahead control ────────────────────────────────────────────────────────

def test_observations_before_is_strictly_exclusive():
    """`<=` here would rank a value against a set containing itself."""
    series = [{"date": d, "vrp": 1.0} for d in
              ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08")]
    got = observations_before(series, "2026-01-07")
    assert [r["date"] for r in got] == ["2026-01-05", "2026-01-06"]
    assert observations_before(series, "2026-01-05") == []


def test_percentile_needs_enough_observations():
    thin = [{"vrp": float(i)} for i in range(10)]
    assert vrp_percentile(thin, 5.0, min_observations=60) is None

    enough = [{"vrp": float(i)} for i in range(100)]
    assert vrp_percentile(enough, 50.0, min_observations=60) is not None


def test_percentile_places_a_value_correctly():
    series = [{"vrp": float(i)} for i in range(100)]
    assert vrp_percentile(series, -10.0, 60) == 0.0
    assert vrp_percentile(series, 200.0, 60) == 100.0
    assert 45.0 <= vrp_percentile(series, 50.0, 60) <= 55.0


# ── The signal ────────────────────────────────────────────────────────────────

def make_snapshot(session_date: str):
    from market_hours import SessionPhase
    from recorder.models import ChainRow, ChainSnapshot
    return ChainSnapshot(
        ts=f"{session_date}T15:30:00+05:30", session_date=session_date,
        session_phase=SessionPhase.END_OF_DAY, symbol=SYM,
        expiry_date="29-10-2026", expiry_epoch=1793000000, spot=24000.0,
        rows=(ChainRow(strike=24000.0, option_type="CE", ltp=100.0),),
    )


def series_ending(session_date: str, n: int = 120, today_vrp: float = 5.0,
                  base=lambda i: (i % 21) * 0.2 - 2.0) -> list[dict]:
    """`n` prior observations plus a reading for `session_date`."""
    import datetime as dt
    d = dt.date.fromisoformat(session_date)
    out = []
    for i in range(n, 0, -1):
        day = d - dt.timedelta(days=i)
        out.append({"date": day.isoformat(), "iv": 0.18, "rv": 0.16,
                    "vrp": round(base(i), 4)})
    out.append({"date": session_date, "iv": 0.20, "rv": 0.15, "vrp": today_vrp})
    return out


def evaluate(session_date: str, series: list[dict], params=None):
    spec = get_signal(SIGNAL_ID)
    ctx = SignalContext(snapshot=make_snapshot(session_date), history=(),
                        params=params or {}, extras={EXTRAS_KEY: series})
    return spec.evaluate(ctx, params)


def test_rich_premium_fires_short_vol():
    d = "2026-06-15"
    res = evaluate(d, series_ending(d, today_vrp=20.0))
    assert res.fired
    assert res.signal.direction is Direction.SHORT_VOL
    assert res.signal.features["vrp_percentile"] >= 80


def test_cheap_premium_fires_long_vol():
    d = "2026-06-15"
    res = evaluate(d, series_ending(d, today_vrp=-20.0))
    assert res.fired
    assert res.signal.direction is Direction.LONG_VOL


def test_middling_premium_does_not_fire():
    """
    Above the noise floor so the percentile band is what rejects it — the floor
    is checked first, and testing through it would not exercise this path.
    """
    d = "2026-06-15"
    res = evaluate(d, series_ending(d, today_vrp=2.0,
                                    base=lambda i: (i % 41) - 20.0))
    assert not res.fired
    assert "percentile" in res.reason


def test_flat_regime_is_refused_despite_an_extreme_percentile():
    """A percentile can be extreme while the spread is inside measurement noise."""
    d = "2026-06-15"
    series = series_ending(d, today_vrp=0.2, base=lambda i: 0.0)
    res = evaluate(d, series, {"min_abs_vrp_points": 0.5})
    assert not res.fired
    assert "measurement noise" in res.reason


def test_thin_history_skips_with_a_countable_reason():
    d = "2026-06-15"
    res = evaluate(d, series_ending(d, n=10, today_vrp=20.0))
    assert not res.fired
    assert "prior observations" in res.reason
    assert res.features["history_size"] == 10


def test_missing_series_is_reported_not_guessed():
    spec = get_signal(SIGNAL_ID)
    ctx = SignalContext(snapshot=make_snapshot("2026-06-15"), history=(), extras={})
    res = spec.evaluate(ctx)
    assert not res.fired and "no VRP series" in res.reason


def test_no_reading_for_today_skips():
    d = "2026-06-15"
    series = [r for r in series_ending(d) if r["date"] != d]
    res = evaluate(d, series)
    assert not res.fired and "no paired IV/RV reading" in res.reason


def test_signal_cannot_see_its_own_or_later_observations():
    """
    The load-bearing look-ahead test. Appending an extreme FUTURE reading must
    not change today's percentile at all.
    """
    d = "2026-06-15"
    base = series_ending(d, today_vrp=6.0)
    with_future = base + [{"date": "2026-06-16", "iv": 0.9, "rv": 0.1, "vrp": 80.0},
                          {"date": "2026-06-17", "iv": 0.9, "rv": 0.1, "vrp": 90.0}]

    a = evaluate(d, base)
    b = evaluate(d, with_future)
    assert a.features["vrp_percentile"] == b.features["vrp_percentile"]
    assert a.features["history_size"] == b.features["history_size"]


def test_thresholds_are_sweepable():
    """History spans -20..+20 so today's +3 sits mid-range and the threshold,
    not the data, decides."""
    d = "2026-06-15"
    series = series_ending(d, today_vrp=3.0, base=lambda i: (i % 41) - 20.0)
    strict = evaluate(d, series, {"rich_percentile": 99.0})
    loose = evaluate(d, series, {"rich_percentile": 50.0})
    assert not strict.fired
    assert loose.fired and loose.signal.direction is Direction.SHORT_VOL


# ── Loading from the app's own stores ────────────────────────────────────────

def test_load_vrp_history_joins_the_real_stores():
    from snapshot_store import init_db, write_atm_iv_many, write_spot_many
    import datetime as dt

    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        init_db(db)
        days = [d.isoformat() for d in
                (dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(90))
                if d.weekday() < 5]

        write_spot_many(db, [(d, SYM, 24000.0 + i * 20) for i, d in enumerate(days)])
        # Two expiries a side of 30 days so constant-maturity interpolation works.
        rows = []
        for i, d in enumerate(days):
            base = dt.date.fromisoformat(d)
            for offset, iv in ((20, 0.18), (45, 0.19)):
                exp = (base + dt.timedelta(days=offset)).strftime("%d-%m-%Y")
                rows.append((d, SYM, exp, iv))
        write_atm_iv_many(db, rows, source="bhavcopy")

        series = load_vrp_history(db, SYM, days=252)
        assert len(series) > 20
        assert all({"date", "iv", "rv", "vrp"} <= set(r) for r in series)
        assert [r["date"] for r in series] == sorted(r["date"] for r in series)

        info = summarise(series)
        assert info["observations"] == len(series)
        assert info["last_date"] == series[-1]["date"]
    finally:
        os.unlink(db)


def test_summarise_reports_emptiness_usefully():
    info = summarise([])
    assert info["observations"] == 0
    assert "bhavcopy" in info["note"]
