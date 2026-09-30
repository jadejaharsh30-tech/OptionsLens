"""
Closing-auction dislocation tests (roadmap item 34).

What would fail silently, and so is pinned here:

  - **The wrong two bars.** The reading is the LAST continuous bar against the
    FIRST post-auction bar, chosen by the phase each bar was tagged with at
    capture. Taking the first continuous bar measures the whole day; taking a
    CAS_WINDOW bar measures a frozen book.
  - **Mixing measures.** Raw and futures-adjusted readings are different
    quantities; a percentile over a series that is one on some dates and the
    other on others is meaningless and looks fine.
  - **A futures roll read as a move.** On monthly expiry day the front future
    changes at 15:30, so "the future's move over the auction" would be the
    spread between two contracts.
  - **Firing more than once a session**, which would count one auction as
    several independent signals.
  - **Live/backtest divergence.** The official close is captured at 15:50, after
    the signal runs; using it in replay would test a signal that cannot exist.
"""
import math
import os
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cas import (  # noqa: E402
    auction_reading, build_cas_series, load_cas_history, measure_history,
    summarise,
)
from market_hours import SessionPhase  # noqa: E402
from recorder.models import ChainRow, ChainSnapshot  # noqa: E402
from signals.base import Direction, SignalContext  # noqa: E402
from signals.cas_signal import EXTRAS_KEY, SIGNAL_ID  # noqa: E402
from signals.registry import get_signal  # noqa: E402

SYM = "NIFTY"
D = "2026-10-05"


def bar(d, hhmm, phase, spot, futures=None, fut_exp="27-10-2026", expiry="06-10-2026"):
    return ChainSnapshot(
        ts=f"{d}T{hhmm}:00+05:30", session_date=d, session_phase=phase,
        symbol=SYM, expiry_date=expiry, expiry_epoch=1, spot=spot,
        futures=futures, futures_expiry=fut_exp if futures else None,
        rows=(ChainRow(strike=24000.0, option_type="CE", ltp=100.0),))


def session(d, pre=24000.0, post=24000.0, f_pre=None, f_post=None,
            post_fut_exp="27-10-2026"):
    """A session's closing half-hour: continuous, frozen auction, post-auction."""
    C, A, P = SessionPhase.CONTINUOUS, SessionPhase.CAS_WINDOW, SessionPhase.POST_CAS
    return [
        bar(d, "15:10", C, pre - 30, f_pre and f_pre - 30),
        bar(d, "15:14", C, pre, f_pre),
        bar(d, "15:20", A, pre, f_pre and f_pre + 5),     # frozen book
        bar(d, "15:35", P, post, f_post, fut_exp=post_fut_exp),
        bar(d, "15:36", P, post + 3, f_post and f_post + 3, fut_exp=post_fut_exp),
    ]


# ── The reading ───────────────────────────────────────────────────────────────

def test_reading_is_last_continuous_against_first_post_auction():
    r = auction_reading(session(D, pre=24000.0, post=24060.0))
    assert r["pre_ts"].endswith("15:14:00+05:30")
    assert r["post_ts"].endswith("15:35:00+05:30")
    assert abs(r["raw_bps"] - math.log(24060 / 24000) * 1e4) < 1e-3


def test_adjusted_measure_removes_the_futures_own_move():
    """
    Cash up 25 bps through the auction, futures up 20 bps over the same window:
    only 5 bps is the auction disagreeing with the derivatives market.
    """
    s0, f0 = 24000.0, 24100.0
    s1 = s0 * math.exp(0.0025)
    f1 = f0 * math.exp(0.0020)
    r = auction_reading(session(D, pre=s0, post=s1, f_pre=f0, f_post=f1))
    assert abs(r["raw_bps"] - 25.0) < 1e-3
    assert abs(r["futures_bps"] - 20.0) < 1e-3
    assert abs(r["adjusted_bps"] - 5.0) < 1e-3


def test_a_futures_roll_is_not_read_as_a_move():
    """Monthly expiry day: the post-auction bar quotes next month's contract."""
    r = auction_reading(session(D, pre=24000, post=24050, f_pre=24010,
                                f_post=24200, post_fut_exp="24-11-2026"))
    assert r["raw_bps"] is not None
    assert r["adjusted_bps"] is None


def test_missing_futures_leave_only_the_raw_measure():
    r = auction_reading(session(D, pre=24000, post=24050))
    assert r["raw_bps"] is not None and r["adjusted_bps"] is None


def test_no_post_auction_bar_means_no_reading():
    bars = [b for b in session(D) if b.session_phase is not SessionPhase.POST_CAS]
    assert auction_reading(bars) is None


def test_a_session_before_cas_went_live_has_no_auction():
    """Before 3 Aug 2026, 15:15-15:35 was ordinary trading, not an auction."""
    assert auction_reading(session("2026-07-31", pre=24000, post=24060)) is None
    assert auction_reading(session("2026-08-03", pre=24000, post=24060)) is not None


def test_snapshots_and_header_rows_give_the_same_reading():
    """The signal reads snapshots, the loader reads chain_meta dicts."""
    snaps = session(D, pre=24000, post=24060, f_pre=24100, f_post=24150)
    dicts = [{"ts": s.ts, "session_date": s.session_date,
              "session_phase": s.session_phase.value, "spot": s.spot,
              "futures": s.futures, "futures_expiry": s.futures_expiry}
             for s in snaps]
    assert auction_reading(snaps) == auction_reading(dicts)


def test_series_is_one_reading_per_session():
    bars = session("2026-10-05", post=24050) + session("2026-10-06", post=23950)
    series = build_cas_series(bars)
    assert [r["date"] for r in series] == ["2026-10-05", "2026-10-06"]
    assert series[0]["raw_bps"] > 0 > series[1]["raw_bps"]


def test_measures_are_never_mixed():
    series = [{"raw_bps": 1.0, "adjusted_bps": None},
              {"raw_bps": 2.0, "adjusted_bps": 0.5}]
    assert measure_history(series, "raw_bps") == [1.0, 2.0]
    assert measure_history(series, "adjusted_bps") == [0.5]
    with pytest.raises(ValueError):
        measure_history(series, "blend")


# ── The loader ────────────────────────────────────────────────────────────────

def test_loader_reads_headers_collapses_expiries_and_checks_the_official_close():
    from recorder.store import init_db, write_eod_close, write_snapshot

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "m.db")
        init_db(db)
        for b in session(D, pre=24000, post=24060, f_pre=24100, f_post=24150):
            write_snapshot(b, db)
            # A second expiry at the same timestamp must not double-count.
            write_snapshot(bar(b.session_date, b.ts[11:16], b.session_phase,
                               b.spot, b.futures, expiry="13-10-2026"), db)
        write_eod_close(D, SYM, 24061.0, "test", "x", db_path=db)

        series = load_cas_history(SYM, db)

    assert len(series) == 1
    r = series[0]
    assert abs(r["raw_bps"] - math.log(24060 / 24000) * 1e4) < 1e-3
    assert r["official_close"] == 24061.0
    assert abs(r["print_vs_official_bps"] - math.log(24060 / 24061) * 1e4) < 1e-3
    info = summarise(series)
    assert info["checked_against_official"] == 1
    assert info["max_print_gap_bps"] < 1.0


def test_loader_on_a_missing_store_is_empty_and_explained():
    with tempfile.TemporaryDirectory() as tmp:
        assert load_cas_history(SYM, os.path.join(tmp, "absent.db")) == []
    assert "15:40" in summarise([])["note"]


# ── The signal ────────────────────────────────────────────────────────────────

def history_before(d: str, n: int = 120, base=lambda i: ((i % 21) - 10) * 1.0):
    """`n` prior sessions of raw readings spread roughly -10..+10 bps."""
    day = date.fromisoformat(d)
    return [{"date": (day - timedelta(days=i)).isoformat(),
             "raw_bps": base(i), "adjusted_bps": base(i) / 2}
            for i in range(n, 0, -1)]


def evaluate(bars, series, params=None):
    spec = get_signal(SIGNAL_ID)
    *hist, current = bars
    ctx = SignalContext(snapshot=current, history=tuple(hist),
                        params=params or {}, extras={EXTRAS_KEY: series})
    return spec.evaluate(ctx, params)


def first_post_bar(bars):
    """Truncate the session at its first POST_CAS bar — where the signal runs."""
    for i, b in enumerate(bars):
        if b.session_phase is SessionPhase.POST_CAS:
            return bars[:i + 1]
    raise AssertionError("no POST_CAS bar")


def test_an_auction_printed_far_above_is_faded_bearish():
    bars = first_post_bar(session(D, pre=24000, post=24000 * math.exp(0.004)))
    res = evaluate(bars, history_before(D))
    assert res.fired
    assert res.signal.direction is Direction.BEARISH
    assert res.signal.features["percentile"] >= 90


def test_an_auction_printed_far_below_is_faded_bullish():
    bars = first_post_bar(session(D, pre=24000, post=24000 * math.exp(-0.004)))
    res = evaluate(bars, history_before(D))
    assert res.fired
    assert res.signal.direction is Direction.BULLISH


def test_it_fires_once_per_session_not_on_every_post_auction_bar():
    bars = session(D, pre=24000, post=24000 * math.exp(0.004))   # ends at 15:36
    res = evaluate(bars, history_before(D))
    assert not res.fired
    assert "first post-auction bar" in res.reason


def test_it_does_not_run_before_the_auction_has_printed():
    bars = [b for b in session(D) if b.session_phase is SessionPhase.CONTINUOUS]
    res = evaluate(bars, history_before(D))
    assert not res.fired


def test_a_window_that_misses_the_last_continuous_bar_says_so():
    bars = first_post_bar(session(D))[-2:]      # CAS_WINDOW + POST_CAS only
    res = evaluate(bars, history_before(D))
    assert not res.fired
    assert "history_window" in res.reason


def test_a_tiny_gap_is_refused_despite_an_extreme_percentile():
    bars = first_post_bar(session(D, pre=24000, post=24000 * math.exp(0.0003)))
    res = evaluate(bars, history_before(D, base=lambda i: 0.0))
    assert not res.fired
    assert "floor" in res.reason


def test_the_adjusted_measure_skips_when_futures_are_missing():
    bars = first_post_bar(session(D, pre=24000, post=24000 * math.exp(0.004)))
    res = evaluate(bars, history_before(D), {"measure": "adjusted_bps"})
    assert not res.fired
    assert "adjusted_bps unavailable" in res.reason


def test_the_adjusted_measure_fires_when_the_auction_disagrees_with_futures():
    """Cash +40 bps through the auction while futures were flat: pure dislocation."""
    bars = first_post_bar(session(D, pre=24000, post=24000 * math.exp(0.004),
                                  f_pre=24100, f_post=24100))
    res = evaluate(bars, history_before(D), {"measure": "adjusted_bps"})
    assert res.fired
    assert res.signal.direction is Direction.BEARISH
    assert abs(res.signal.features["adjusted_bps"] - 40.0) < 1e-2


def test_todays_reading_is_excluded_from_its_own_percentile():
    bars = first_post_bar(session(D, pre=24000, post=24000 * math.exp(0.004)))
    series = history_before(D) + [{"date": D, "raw_bps": 40.0, "adjusted_bps": None}]
    res = evaluate(bars, series)
    assert res.signal.features["history_size"] == 120


def test_thin_history_skips_with_a_countable_reason():
    bars = first_post_bar(session(D, pre=24000, post=24000 * math.exp(0.004)))
    res = evaluate(bars, history_before(D, n=15))
    assert not res.fired
    assert "only 15 prior sessions" in res.reason


def test_the_signal_declares_daily_labelling():
    assert get_signal(SIGNAL_ID).label_mode == "daily"


# ── Through the backtester ────────────────────────────────────────────────────

def test_intraday_bars_are_scored_on_daily_horizons_because_the_signal_says_so():
    """
    Without the declared horizon the engine would see minute bars and score
    5-60 minute horizons that all fall after the session has closed.
    """
    from backtest.engine import run_backtest
    from recorder.store import init_db, write_snapshots

    days, d = [], date(2026, 8, 3)
    while len(days) < 90:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "m.db")
        init_db(db)
        spot = 24000.0
        batch = []
        for i, day in enumerate(days):
            # Most auctions are small; every tenth prints +45 bps.
            gap = 0.0045 if i % 10 == 9 else ((i % 7) - 3) * 0.0002
            batch.extend(session(day, pre=spot, post=spot * math.exp(gap)))
            spot *= 1.0 + ((i % 5) - 2) * 0.001
        write_snapshots(batch, db)

        series = load_cas_history(SYM, db)
        run = run_backtest(SIGNAL_ID, SYM, db_path=db, persist_evaluations=False,
                           extras={EXTRAS_KEY: series},
                           params={"min_observations": 40}).to_dict()

    assert run["label_mode"] == "daily"
    assert run["signals_fired"] > 0
    assert set(run["horizons"]) == {"1d", "5d", "20d"}
