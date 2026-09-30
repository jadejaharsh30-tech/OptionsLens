"""
`python -m backtest.cli` end to end, on a synthetic archive and app store.

This is the path a user takes to get a first verdict from the bhavcopy history,
so it is tested as a user would run it: archive in, printed table out. The
properties that matter:

  - the archive is materialised into a SEPARATE snapshot file, one bar per
    session, and only missing dates are added on a re-run;
  - the signal gets the same series the API would build;
  - a vol signal comes back scored in vol points, with the null beside it.
"""
import datetime as dt
import math
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import RISK_FREE_RATE  # noqa: E402
from iv_engine import black76_price  # noqa: E402

SYM = "NIFTY"


def weekdays(n, start=dt.date(2025, 1, 6)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def build_world(tmp: str, days: list[dt.date]) -> tuple[str, str]:
    """An archive with a priceable front chain per day, and an app store with
    per-expiry ATM IV either side of 30 days plus exchange closes."""
    from bhavcopy.download import SCHEMA
    from snapshot_store import init_db, write_atm_iv_many, write_spot_many

    archive = os.path.join(tmp, "nse.db")
    app = os.path.join(tmp, "app.db")
    conn = sqlite3.connect(archive)
    conn.executescript(SCHEMA)
    init_db(app)

    spot = 24000.0
    iv_rows, spot_rows = [], []            # batched: one commit, not thousands
    for i, d in enumerate(days):
        # A slow IV cycle so the VRP spread visits both tails.
        iv = 0.14 + 0.05 * math.sin(i / 9.0)
        expiry = d + dt.timedelta(days=7)
        T = 7 / 365
        for k in (spot - 100, spot, spot + 100):
            for opt in ("CE", "PE"):
                px = black76_price(spot, k, T, RISK_FREE_RATE, iv, opt)
                conn.execute("""
                    INSERT INTO daily_option (trad_dt, symbol, expiry_dt, strike,
                        option_type, close, volume, oi, chg_oi, underlying, era)
                    VALUES (?, ?, ?, ?, ?, ?, 500, 1000, 10, ?, 'udiff')
                """, (d.isoformat(), SYM, expiry.isoformat(), k, opt,
                      round(px, 2), spot))
        for offset in (20, 40):
            iv_rows.append((d.isoformat(), SYM,
                            (d + dt.timedelta(days=offset)).strftime("%d-%m-%Y"), iv))
        spot_rows.append((d.isoformat(), SYM, spot))
        spot *= 1.0 + 0.004 * math.sin(i * 1.7)
    write_atm_iv_many(app, iv_rows, "bhavcopy")
    write_spot_many(app, spot_rows)
    conn.commit()
    conn.close()
    return archive, app


def test_run_materialises_history_and_prints_a_vol_verdict(capsys):
    from backtest.cli import main

    with tempfile.TemporaryDirectory() as tmp:
        archive, app = build_world(tmp, weekdays(160))
        snaps = os.path.join(tmp, "eod_snapshots.db")
        base = ["--db", snaps, "--app-db", app, "--archive", archive]

        assert main(base + ["run", "--signal", "vrp", "--symbol", SYM]) == 0
        out = capsys.readouterr().out
        assert "160 sessions (160 added this run)" in out
        assert "unit: vol pts" in out
        assert "VRP history:" in out                   # the API's own note
        assert "verdict" in out and "20d" in out

        # A re-run adds nothing: only missing dates are materialised.
        assert main(base + ["run", "--signal", "vrp", "--symbol", SYM]) == 0
        assert "(0 added this run)" in capsys.readouterr().out


def test_params_are_passed_through_and_typed(capsys):
    from backtest.cli import _parse_params, main

    assert _parse_params(["a=1", "b=2.5", "c=true", "mode=momentum"]) == {
        "a": 1, "b": 2.5, "c": True, "mode": "momentum"}

    with tempfile.TemporaryDirectory() as tmp:
        archive, app = build_world(tmp, weekdays(120))
        base = ["--db", os.path.join(tmp, "s.db"), "--app-db", app,
                "--archive", archive]
        assert main(base + ["run", "--signal", "vrp",
                            "--param", "rich_percentile=95"]) == 0
    assert "'rich_percentile': 95" in capsys.readouterr().out


def test_correlation_runs_over_the_same_history(capsys):
    from backtest.cli import main

    with tempfile.TemporaryDirectory() as tmp:
        archive, app = build_world(tmp, weekdays(160))
        base = ["--db", os.path.join(tmp, "s.db"), "--app-db", app,
                "--archive", archive]
        assert main(base + ["correlation", "--signals", "vrp,term_structure"]) == 0
    out = capsys.readouterr().out
    assert "vrp × term_structure" in out or "term_structure × vrp" in out


def test_insignificance_notes_name_the_unit_actually_measured():
    """
    Vol mode stores vol points in the `*_bps` fields. A note reading "2.8 bps"
    for a 2.8 vol-point outcome misstates it by two orders of magnitude.
    """
    from backtest.metrics import BacktestStats, HorizonStats, interpret

    stats = BacktestStats(signal_id="x", version=1, n_signals=40, by_horizon={
        "20d_vol": HorizonStats(horizon="20d_vol", n=40, mean_bps=2.8, t_stat=1.2)})
    assert "2.8 vol points is" in interpret(stats, unit="vol_points")[0]
    assert "2.8 bps is" in interpret(stats)[0]


def test_unknown_signal_and_empty_archive_fail_with_a_message(capsys):
    from backtest.cli import main
    from bhavcopy.download import SCHEMA

    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "empty.db")
        conn = sqlite3.connect(archive)
        conn.executescript(SCHEMA)
        conn.close()
        base = ["--db", os.path.join(tmp, "s.db"), "--archive", archive]

        assert main(base + ["run", "--signal", "not_a_signal"]) == 2
        assert main(base + ["run", "--signal", "vrp"]) == 1
    assert "BHAVCOPY.md" in capsys.readouterr().err


# ── Long history: the out-of-sample run on 2008-2024 ──────────────────────────

def test_sessions_older_than_two_years_still_get_their_history(capsys):
    """
    The loaders kept only the most recent 504 dates. On an 18-year archive
    every session before the last two years found no reading and skipped, so
    an out-of-sample run on the old years would have tested nothing.
    """
    from backtest.cli import main

    with tempfile.TemporaryDirectory() as tmp:
        archive, app = build_world(tmp, weekdays(760))
        days = weekdays(760)
        cutoff = days[199].isoformat()                 # 560 dates before the end
        base = ["--db", os.path.join(tmp, "s.db"), "--app-db", app,
                "--archive", archive]
        assert main(base + ["run", "--signal", "vrp", "--to", cutoff]) == 0
    out = capsys.readouterr().out
    assert f"Replaying 200 sessions, {days[0].isoformat()} to {cutoff}." in out
    assert "vrp.v1 on NIFTY — 200 sessions" in out
    line = next(l for l in out.splitlines() if l.startswith("vrp.v1 on NIFTY"))
    fired = int(line.split("sessions, ")[1].split(" fired")[0])
    assert fired > 0


def test_ranking_uses_the_stated_lookback_not_all_prior_history():
    """
    Loading the full archive must not silently turn a two-year rank into an
    eighteen-year one: that would be a different signal.
    """
    from vrp import DEFAULT_LOOKBACK, recent_observations

    series = [{"date": (dt.date(2008, 1, 1) + dt.timedelta(days=i)).isoformat(),
               "vrp": float(i)} for i in range(2000)]
    got = recent_observations(series, "2013-06-01")
    assert len(got) == DEFAULT_LOOKBACK
    assert got[-1]["date"] < "2013-06-01"
    assert len(recent_observations(series, "2013-06-01", lookback=60)) == 60
    assert len(recent_observations(series, "2008-01-05")) == 4   # fewer exist
