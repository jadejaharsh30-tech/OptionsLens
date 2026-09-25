"""
Vol-outcome labelling tests.

The property that matters: a SHORT_VOL position is scored by whether implied
exceeded SUBSEQUENTLY realised volatility, not by which way the index went. The
directional labeller answers a question these signals never asked, and returned
NO_EDGE for reasons unrelated to whether they work.

Also pinned here: the label is ex-post (vol realised AFTER entry), never the
contemporaneous spread the VRP signal reads as its input. Using the same
quantity as both input and outcome would be circular.
"""
import math
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.vol_labels import (  # noqa: E402
    HORIZONS_VOL_SESSIONS, build_vol_null_labels, compute_vol_outcome,
    direction_sign, forward_realised_vol, iv_by_date_from_extras,
    vol_horizon_keys,
)
from realized_vol import compute_realized_vol  # noqa: E402

SYM = "NIFTY"


def sessions(n: int) -> list[str]:
    import datetime as dt
    out, d = [], dt.date(2026, 1, 5)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def constant_vol_closes(n: int, daily_vol: float, start=24000.0) -> list[float]:
    """Alternating returns of fixed magnitude — realised vol is deterministic."""
    closes = [start]
    for i in range(1, n):
        closes.append(closes[-1] * math.exp(daily_vol * (1 if i % 2 else -1)))
    return closes


# ── Forward realised vol ─────────────────────────────────────────────────────

def test_forward_vol_uses_only_sessions_after_entry():
    """A calm run then a violent one: entering at the boundary must see the violence."""
    calm = [100.0] * 21
    violent = constant_vol_closes(21, 0.03, start=100.0)
    closes = calm + violent[1:]

    rv = forward_realised_vol(closes, index=20, sessions=20)
    assert rv is not None and rv > 0.3          # sees the violent half only

    rv_before = forward_realised_vol(closes, index=0, sessions=20)
    assert rv_before == 0.0                     # the calm half, zero movement


def test_forward_vol_past_the_end_is_none():
    closes = [100.0 + i for i in range(10)]
    assert forward_realised_vol(closes, index=5, sessions=20) is None
    assert forward_realised_vol(closes, index=0, sessions=9) is not None


def test_forward_vol_matches_the_estimator():
    closes = constant_vol_closes(40, 0.01)
    direct = compute_realized_vol(closes[10:31], window=20)
    assert abs(forward_realised_vol(closes, 10, 20) - direct) < 1e-12


# ── The signed label ─────────────────────────────────────────────────────────

def test_short_vol_is_right_when_implied_exceeded_realised():
    ds = sessions(40)
    closes = constant_vol_closes(40, 0.005)      # ~8% annualised
    iv = {d: 0.20 for d in ds}                   # sold at 20%

    lab = compute_vol_outcome(0, ds, closes, iv, SYM, "SHORT_VOL")
    assert lab.returns_bps["20d_vol"] > 0        # implied was richer: correct


def test_long_vol_sign_is_the_mirror():
    ds = sessions(40)
    closes = constant_vol_closes(40, 0.005)
    iv = {d: 0.20 for d in ds}

    short = compute_vol_outcome(0, ds, closes, iv, SYM, "SHORT_VOL")
    long_ = compute_vol_outcome(0, ds, closes, iv, SYM, "LONG_VOL")
    assert short.returns_bps["20d_vol"] == -long_.returns_bps["20d_vol"]


def test_long_vol_is_right_when_realised_exceeded_implied():
    ds = sessions(40)
    closes = constant_vol_closes(40, 0.03)       # ~48% annualised
    iv = {d: 0.15 for d in ds}                   # bought at 15%

    lab = compute_vol_outcome(0, ds, closes, iv, SYM, "LONG_VOL")
    assert lab.returns_bps["20d_vol"] > 0


def test_label_is_in_vol_points():
    ds = sessions(40)
    closes = [100.0] * 40                        # zero realised vol
    iv = {d: 0.18 for d in ds}
    lab = compute_vol_outcome(0, ds, closes, iv, SYM, "SHORT_VOL")
    assert abs(lab.returns_bps["20d_vol"] - 18.0) < 1e-6   # 18 vol points, not bps


def test_direction_is_ignored_by_the_underlying_path():
    """
    The whole point: a strong TREND with low vol must score the same as a flat
    market with the same vol. Direction cannot leak into a vol outcome.
    """
    ds = sessions(40)
    flat = [100.0] * 40
    trending = [100.0 * (1.004 ** i) for i in range(40)]   # rises ~17%
    iv = {d: 0.18 for d in ds}

    a = compute_vol_outcome(0, ds, flat, iv, SYM, "SHORT_VOL")
    b = compute_vol_outcome(0, ds, trending, iv, SYM, "SHORT_VOL")
    # A steady drift has near-zero close-to-close variance, so both are ~18.
    assert abs(a.returns_bps["20d_vol"] - b.returns_bps["20d_vol"]) < 0.5


def test_all_horizons_present_and_named():
    ds = sessions(40)
    closes = constant_vol_closes(40, 0.01)
    iv = {d: 0.2 for d in ds}
    lab = compute_vol_outcome(0, ds, closes, iv, SYM)
    assert set(lab.returns_bps) == set(vol_horizon_keys())
    assert vol_horizon_keys() == ["5d_vol", "10d_vol", "20d_vol"]


def test_horizon_past_the_data_is_none():
    ds = sessions(12)
    closes = constant_vol_closes(12, 0.01)
    iv = {d: 0.2 for d in ds}
    lab = compute_vol_outcome(0, ds, closes, iv, SYM)
    assert lab.returns_bps["5d_vol"] is not None
    assert lab.returns_bps["20d_vol"] is None


def test_missing_entry_iv_yields_no_label():
    """A vol outcome without an entry implied level is not a vol outcome."""
    ds = sessions(40)
    closes = constant_vol_closes(40, 0.01)
    assert compute_vol_outcome(0, ds, closes, {}, SYM) is None


def test_entry_iv_is_recorded_in_vol_points():
    ds = sessions(40)
    closes = constant_vol_closes(40, 0.01)
    lab = compute_vol_outcome(0, ds, closes, {d: 0.175 for d in ds}, SYM)
    assert abs(lab.spot_at_signal - 17.5) < 1e-9


def test_path_track_brackets_the_final_miss():
    """The vol analogue of drawdown: was the position underwater partway?"""
    ds = sessions(40)
    # Violent first, then calm: a short-vol entry is underwater early.
    closes = constant_vol_closes(11, 0.04) + [closes_ for closes_ in
                                              [constant_vol_closes(11, 0.04)[-1]] * 29]
    iv = {d: 0.30 for d in ds}
    lab = compute_vol_outcome(0, ds, closes, iv, SYM, "SHORT_VOL")
    assert lab.mae_bps["20d_vol"] <= lab.returns_bps["20d_vol"] <= lab.mfe_bps["20d_vol"]
    assert lab.mae_bps["20d_vol"] < 0        # underwater at some point


def test_direction_sign_mapping():
    assert direction_sign("SHORT_VOL") == 1
    assert direction_sign("LONG_VOL") == -1


# ── Null ──────────────────────────────────────────────────────────────────────

def test_null_labels_use_their_own_entry_iv():
    ds = sessions(40)
    closes = constant_vol_closes(40, 0.005)
    iv = {d: (0.10 if i < 20 else 0.40) for i, d in enumerate(ds)}

    labels = build_vol_null_labels([0, 20], ds, closes, iv, SYM,
                                   directions=["SHORT_VOL", "SHORT_VOL"])
    assert len(labels) == 2
    # Entry at the high-IV date must score far better for a vol seller.
    assert labels[1].returns_bps["10d_vol"] > labels[0].returns_bps["10d_vol"]


def test_null_skips_dates_without_iv():
    ds = sessions(40)
    closes = constant_vol_closes(40, 0.01)
    iv = {ds[0]: 0.2}
    labels = build_vol_null_labels([0, 1, 2], ds, closes, iv, SYM)
    assert len(labels) == 1


# ── Extras plumbing ──────────────────────────────────────────────────────────

def test_iv_series_read_from_either_extras_key():
    direct = iv_by_date_from_extras({"iv_series": [{"date": "d", "iv": 0.2}]})
    viavrp = iv_by_date_from_extras({"vrp_series": [{"date": "d", "iv": 0.2,
                                                     "rv": 0.1, "vrp": 10.0}]})
    assert direct == viavrp == {"d": 0.2}
    assert iv_by_date_from_extras(None) == {}
    assert iv_by_date_from_extras({}) == {}


# ── Engine integration ───────────────────────────────────────────────────────

def _vol_db_and_series(n=160):
    """One EOD bar per session, plus a matching IV series."""
    import datetime as dt
    import random
    from market_hours import SessionPhase
    from recorder.models import ChainRow, ChainSnapshot
    from recorder.store import init_db, write_snapshot
    from signals.store import init_db as init_sig

    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(db)
    init_sig(db)

    rng = random.Random(11)
    ds = sessions(n)
    spot, level = 24000.0, 0.16
    iv_series = []
    for i, d in enumerate(ds):
        level += (0.16 - level) * 0.05 + rng.gauss(0, 0.005)
        level = max(0.08, min(0.35, level))
        iv_series.append({"date": d, "iv": level})
        spot *= math.exp(rng.gauss(0, level / math.sqrt(252)))
        rows = []
        for k in range(-4, 5):
            strike = round((spot + k * 100) / 50) * 50.0
            for opt in ("CE", "PE"):
                skew = (max(0.0, strike - spot) if opt == "CE"
                        else max(0.0, spot - strike)) / 10
                rows.append(ChainRow(strike=strike, option_type=opt,
                                     oi=1000.0 + skew * 20, ltp=120.0,
                                     volume=600.0))
        write_snapshot(ChainSnapshot(
            ts=f"{d}T15:30:00+05:30", session_date=d,
            session_phase=SessionPhase.END_OF_DAY, symbol=SYM,
            expiry_date="31-12-2026", expiry_epoch=1798700000,
            spot=spot, rows=tuple(rows)), db)
    return db, iv_series


def test_engine_scores_a_vol_signal_on_vol_not_direction():
    from backtest.engine import run_backtest
    import signals.library  # noqa: F401

    db, iv_series = _vol_db_and_series()
    try:
        out = run_backtest("gex_regime", SYM, db_path=db,
                           extras={"iv_series": iv_series}).to_dict()
        assert out["label_mode"] == "vol"
        assert out["unit"] == "vol_points"
        assert set(out["horizons"]) == set(vol_horizon_keys())
        assert any("VOL POINTS" in n for n in out["notes"])
    finally:
        os.unlink(db)


def test_vol_mode_without_an_iv_series_says_so():
    """A missing series must be an explained failure, not a silent empty table."""
    from backtest.engine import run_backtest
    import signals.library  # noqa: F401

    db, _ = _vol_db_and_series(n=60)
    try:
        out = run_backtest("gex_regime", SYM, db_path=db).to_dict()
        assert out["label_mode"] == "vol"
        assert any("No IV series supplied" in n for n in out["notes"])
    finally:
        os.unlink(db)
