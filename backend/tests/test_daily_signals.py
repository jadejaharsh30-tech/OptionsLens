"""
Daily signal runner tests (roadmap item 52, signals half).

What would fail silently, and so is pinned here:

  - **A signal left out of the daily run.** Discovered from the registry, so
    a new series-backed signal joins without a list being edited.
  - **The runner computing something the backtester would not.** The fire
    here comes through `replay_session` and `build_extras_for`, the
    backtester's own path.
  - **A rerun rewriting history.** The first evaluation of a session is kept.
  - **The digest sent twice, or marked sent when nothing delivered it.**
  - **The windowed skew load ranking against a different history from the
    full load.** It must be exactly the same readings, not approximately.
"""
import asyncio
import datetime as dt
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import daily_signals as ds  # noqa: E402
from market_hours import SessionPhase  # noqa: E402
from notify.base import Dispatcher, Notification, NotificationChannel  # noqa: E402
from recorder.models import ChainRow, ChainSnapshot  # noqa: E402
from signals.base import Direction, Signal, SignalResult  # noqa: E402

DAY = "2026-06-30"          # a Tuesday
SYM = "NIFTY"


class FakeChannel(NotificationChannel):
    def __init__(self, configured=True):
        self._configured = configured
        self.sent: list[Notification] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def configured(self) -> bool:
        return self._configured

    async def send(self, notification: Notification) -> bool:
        self.sent.append(notification)
        return True


def weekdays_ending(day: str, n: int) -> list[str]:
    out, d = [], dt.date.fromisoformat(day)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return out[::-1]


def eod_bar(day: str, symbol: str = SYM) -> ChainSnapshot:
    return ChainSnapshot(
        ts=f"{day}T15:30:00+05:30", session_date=day,
        session_phase=SessionPhase.END_OF_DAY, symbol=symbol,
        expiry_date="28-07-2026", expiry_epoch=1785232800, spot=24000.0,
        rows=(ChainRow(strike=24000.0, option_type="CE", ltp=300.0),
              ChainRow(strike=24000.0, option_type="PE", ltp=280.0)))


@pytest.fixture
def stores():
    """
    An app store whose VRP on DAY is far above its history (IV jumps from 18%
    to 40% while realised stays near 8%), and an EOD bar for DAY.
    """
    from recorder.store import init_db as init_recorder_db, write_snapshots
    from snapshot_store import init_db, write_atm_iv_many, write_spot_many

    tmp = tempfile.mkdtemp()
    paths = {k: os.path.join(tmp, f"{k}.db") for k in ("app", "snap", "eval", "archive")}
    init_db(paths["app"])

    days = weekdays_ending(DAY, 150)
    write_spot_many(paths["app"], [(d, SYM, 24000.0 * (1.005 if i % 2 else 1.0))
                                   for i, d in enumerate(days)])
    rows = []
    for d in days:
        iv = 0.40 if d == DAY else 0.18
        for offset in (20, 45):
            exp = (dt.date.fromisoformat(d) + dt.timedelta(days=offset)).strftime("%d-%m-%Y")
            rows.append((d, SYM, exp, iv))
    write_atm_iv_many(paths["app"], rows, source="bhavcopy")

    init_recorder_db(paths["snap"])
    write_snapshots([eod_bar(DAY)], db_path=paths["snap"])
    return paths


def run(paths, **kw):
    return ds.run_daily(DAY, "eod", [SYM], app_db=paths["app"],
                        snapshot_db=paths["snap"], eval_db=paths["eval"],
                        archive_db=paths["archive"], **kw)


def by_signal(report):
    return {e.signal_id: e for e in report.evaluations}


# ── Which signals ─────────────────────────────────────────────────────────────

def test_every_series_backed_signal_runs_and_no_intraday_one_does():
    ids = {s.signal_id for s in ds.daily_signal_specs()}
    assert {"vrp", "term_structure", "dispersion", "skew_rr25",
            "cas_dislocation"} <= ids
    assert not ids & {"gex_regime", "oi_buildup"}


# ── The run ───────────────────────────────────────────────────────────────────

def test_a_rich_premium_fires_through_the_backtesters_path(stores):
    report = run(stores)
    vrp = by_signal(report)["vrp"]
    assert vrp.status == ds.FIRED
    assert vrp.direction == Direction.SHORT_VOL.value
    assert vrp.result.features["vrp_percentile"] == 100.0
    assert vrp.result.ts.startswith(DAY)


def test_signals_without_their_series_are_unavailable_with_a_reason(stores):
    sig = by_signal(run(stores))
    # No basket members imported, no 60-day leg, a chain with no wings, and an
    # end-of-day bar is not an auction.
    for name in ("dispersion", "term_structure", "skew_rr25", "cas_dislocation"):
        assert sig[name].status == ds.UNAVAILABLE, name
        assert sig[name].reason, name


def test_a_session_with_no_bar_is_reported_not_evaluated(stores):
    report = ds.run_daily("2026-07-01", "eod", [SYM], app_db=stores["app"],
                          snapshot_db=stores["snap"], eval_db=stores["eval"],
                          archive_db=stores["archive"])
    assert all(e.status == ds.UNAVAILABLE and e.result is None
               for e in report.evaluations)
    assert "no eod bar for 2026-07-01" in report.evaluations[0].reason
    _, body = ds.format_digest(report)
    assert f"{SYM} (all signals): no eod bar" in body
    assert not os.path.exists(stores["archive"])      # read, never created


def test_the_eod_bar_is_materialised_from_the_archive(stores, tmp_path):
    """The at-home path: today's bar exists only in the downloaded archive."""
    from bhavcopy.download import connect, insert
    from iv_engine import black76_price
    from market_hours import time_to_expiry
    from recorder.store import recorded_dates

    day, expiry, spot = "2026-07-01", "2026-07-28", 24000.0
    T = time_to_expiry("28-07-2026",
                       now=dt.datetime.fromisoformat(f"{day}T15:30:00+05:30"))
    cols = ["trad_dt", "symbol", "expiry_dt", "strike", "option_type", "close",
            "underlying", "oi", "volume", "lot_size", "instrument", "era"]
    records = [
        dict(trad_dt=day, symbol=SYM, expiry_dt=expiry, strike=k, option_type=o,
             close=round(black76_price(spot * 1.005, k, T, 0.065, 0.16, o), 2),
             underlying=spot, oi=1000.0, volume=500.0, lot_size=65.0,
             instrument="OPTIDX", era="udiff")
        for k in (23800.0, 23900.0, 24000.0, 24100.0, 24200.0) for o in ("CE", "PE")]
    archive = tmp_path / "archive.db"
    conn = connect(archive)
    insert(conn, "daily_option", cols, records)
    conn.commit()
    conn.close()

    assert day not in recorded_dates(SYM, db_path=stores["snap"])
    report = ds.run_daily(day, "eod", [SYM], app_db=stores["app"],
                          snapshot_db=stores["snap"], eval_db=stores["eval"],
                          archive_db=str(archive), persist=False)
    assert day in recorded_dates(SYM, db_path=stores["snap"])
    assert all(e.result is not None for e in report.evaluations)
    assert ds.latest_eod_date([SYM], str(archive)) == day


def test_a_missing_recorder_store_is_unavailable_not_created(stores, tmp_path):
    missing = str(tmp_path / "nope.db")
    report = ds.run_daily(DAY, "recorder", [SYM], app_db=stores["app"],
                          snapshot_db=missing, eval_db=stores["eval"])
    assert all(e.status == ds.UNAVAILABLE for e in report.evaluations)
    assert not os.path.exists(missing)


# ── Choosing one result per session ──────────────────────────────────────────

def result(fired: bool, ts: str) -> SignalResult:
    if fired:
        return SignalResult.fire(Signal("x", 1, ts, SYM, Direction.SHORT_VOL, 0.5))
    return SignalResult(signal_id="x", version=1, ts=ts, symbol=SYM,
                        fired=False, reason="quiet")


def test_the_last_fired_bar_wins_else_the_last_bar():
    bars = [result(False, "09:15"), result(True, "15:35"), result(False, "15:40")]
    assert ds.choose_result(bars).ts == "15:35"          # the auction bar
    assert ds.choose_result([result(True, "a"), result(True, "b")]).ts == "b"
    assert ds.choose_result([result(False, "a"), result(False, "b")]).ts == "b"
    assert ds.choose_result([]) is None


def test_features_separate_quiet_from_unavailable():
    quiet = SignalResult("x", 1, "t", SYM, False, features={"p": 50}, reason="mid")
    blank = SignalResult("x", 1, "t", SYM, False, reason="no series")
    assert ds.classify(quiet) == ds.QUIET
    assert ds.classify(blank) == ds.UNAVAILABLE


# ── Persistence ───────────────────────────────────────────────────────────────

def logged(paths):
    conn = sqlite3.connect(paths["eval"])
    try:
        return conn.execute(
            "SELECT signal_id, fired FROM signal_evaluations WHERE run_id = ?",
            (ds.run_id_for(DAY, "eod"),)).fetchall()
    finally:
        conn.close()


def test_every_evaluation_is_logged_and_a_rerun_adds_nothing(stores):
    report = run(stores)
    first = logged(stores)
    assert len(first) == len(report.evaluations)
    assert ("vrp", 1) in first

    run(stores)
    assert sorted(logged(stores)) == sorted(first)


def test_persist_false_writes_nothing(stores):
    run(stores, persist=False)
    assert not os.path.exists(stores["eval"])


# ── The digest ────────────────────────────────────────────────────────────────

def test_digest_labels_every_fire_unvalidated(stores):
    title, body = ds.format_digest(run(stores, persist=False))
    assert "1 fired" in title
    assert f"{SYM} · vrp v1 → SHORT_VOL" in body
    assert "[unvalidated]" in body
    assert "not trade advice" in body


def test_a_validated_signal_is_labelled_as_such(stores, monkeypatch):
    monkeypatch.setattr(ds, "VALIDATED", frozenset({"vrp"}))
    _, body = ds.format_digest(run(stores, persist=False))
    assert "[validated]" in body and "Validated out of sample: vrp" in body


def test_a_quiet_day_still_produces_a_digest(stores):
    report = ds.run_daily("2026-07-01", "eod", [SYM], app_db=stores["app"],
                          snapshot_db=stores["snap"], eval_db=stores["eval"],
                          archive_db=stores["archive"], persist=False)
    note = ds.digest_notification(report)
    assert "nothing fired" in note.title
    assert note.severity.value == "INFO"


def test_digest_is_sent_once_per_session(stores):
    report = run(stores)
    chan = FakeChannel()
    first = asyncio.run(ds.notify_report(report, stores["eval"],
                                         dispatcher=Dispatcher([chan])))
    assert first["sent"] and len(chan.sent) == 1
    assert chan.sent[0].severity.value == "SIGNAL"

    again = asyncio.run(ds.notify_report(report, stores["eval"],
                                         dispatcher=Dispatcher([chan])))
    assert not again["sent"] and "already sent" in again["reason"]
    assert len(chan.sent) == 1

    forced = asyncio.run(ds.notify_report(report, stores["eval"], force=True,
                                          dispatcher=Dispatcher([chan])))
    assert forced["sent"] and len(chan.sent) == 2


def test_an_unconfigured_channel_does_not_mark_the_session_sent(stores):
    report = run(stores)
    out = asyncio.run(ds.notify_report(
        report, stores["eval"], dispatcher=Dispatcher([FakeChannel(configured=False)])))
    assert not out["sent"] and "TELEGRAM_BOT_TOKEN" in out["reason"]
    assert ds.notified_at(DAY, "eod", stores["eval"]) is None


# ── The windowed skew load ────────────────────────────────────────────────────

def test_windowed_skew_load_ranks_against_exactly_the_full_history(monkeypatch):
    """Walking back from `until` must reproduce the full load's ranking window."""
    import recorder.store as rs
    import skew
    from vrp import recent_observations

    days = weekdays_ending(DAY, 40)
    # Some sessions yield no reading, so "N readings" and "N dates" differ.
    values = {d: (None if i % 4 == 1 else round(-2.0 + i * 0.05, 4))
              for i, d in enumerate(days)}

    class Bar:                       # all build_skew_series reads off a snapshot
        def __init__(self, d):
            self.session_date, self.ts = d, f"{d}T15:30:00+05:30"

    monkeypatch.setattr(rs, "recorded_dates", lambda symbol, **kw: list(days))
    monkeypatch.setattr(rs, "last_snapshot_of_session",
                        lambda symbol, d, **kw: Bar(d))
    monkeypatch.setattr(skew, "rr25_from_snapshot",
                        lambda snap: values[snap.session_date])

    full = skew.load_skew_history(None, SYM)
    for until in (days[-1], days[-6], days[-3]):
        for k in (5, 10, 25):
            window = skew.load_skew_history(None, SYM, until=until,
                                            max_prior_readings=k)
            assert recent_observations(window, until, k) == \
                recent_observations(full, until, k)
            assert ([r for r in window if r["date"] == until] ==
                    [r for r in full if r["date"] == until])
            assert all(r["date"] <= until for r in window)


# ── Command line ──────────────────────────────────────────────────────────────

def test_cli_prints_the_digest(stores, monkeypatch, capsys):
    import config
    monkeypatch.setattr(config, "DB_PATH", stores["app"])
    monkeypatch.setattr(config, "MARKET_DATA_DB", stores["eval"])
    monkeypatch.setattr(config, "NSE_EOD_DB", stores["archive"])

    code = ds.main(["--source", "eod", "--date", DAY, "--symbols", SYM,
                    "--snapshot-db", stores["snap"]])
    out = capsys.readouterr().out
    assert code == 0
    assert "FIRED" in out and "vrp" in out


def test_cli_eod_without_an_archive_says_what_to_run(stores, monkeypatch, capsys):
    import config
    monkeypatch.setattr(config, "NSE_EOD_DB", stores["archive"])
    assert ds.main(["--source", "eod", "--symbols", SYM]) == 1
    assert "Run the bhavcopy download" in capsys.readouterr().out
