"""
Signal correlation report tests (roadmap item 36, the ensemble prerequisite).

The failure this exists to prevent is counting one observation several times:
two signals driven by the same regime agree because they are the same
measurement, and an ensemble that adds their votes overstates its confidence.
So the properties pinned here are:

  - identical and mirror-image signals are both REDUNDANT (a signal and its
    negation carry one observation, not two);
  - SHORT_VOL and BEARISH are never correlated as if they were one claim;
  - only sessions BOTH signals were evaluated on are compared — "not
    evaluated" is not "silent";
  - the replay used is the backtester's own, so the fires counted here are the
    fires a backtest would label.
"""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.correlation import (  # noqa: E402
    compare_pair, correlation_report, family, session_activity,
)
from market_hours import SessionPhase  # noqa: E402
from recorder.models import ChainRow, ChainSnapshot  # noqa: E402
from signals.base import Direction, Signal, SignalResult  # noqa: E402
from signals.registry import _REGISTRY, register_signal  # noqa: E402

DATES = [f"2026-03-{d:02d}" for d in range(2, 32)]


def act(pattern: dict[int, str], dates=DATES) -> dict:
    """{date: direction or None}, firing on the given date indices."""
    return {d: pattern.get(i) for i, d in enumerate(dates)}


# ── Families ──────────────────────────────────────────────────────────────────

def test_family_is_read_from_what_the_signal_emitted():
    assert family(act({0: "SHORT_VOL", 3: "LONG_VOL"})) == "vol"
    assert family(act({0: "BULLISH"})) == "directional"
    assert family(act({0: "BULLISH", 1: "SHORT_VOL"})) == "mixed"
    assert family(act({})) is None


# ── Pairs ─────────────────────────────────────────────────────────────────────

EVERY_THIRD_SHORT = {i: "SHORT_VOL" for i in range(0, 30, 3)}


def test_a_signal_compared_with_itself_is_redundant():
    a = act(EVERY_THIRD_SHORT)
    p = compare_pair(a, a)
    assert p["verdict"] == "REDUNDANT"
    assert p["signed_corr"] == 1.0
    assert p["agreement"] == 1.0


def test_a_mirror_image_is_also_redundant():
    """Always opposite is one observation read two ways, not two observations."""
    a = act(EVERY_THIRD_SHORT)
    b = act({i: "LONG_VOL" for i in EVERY_THIRD_SHORT})
    p = compare_pair(a, b)
    assert p["signed_corr"] == -1.0
    assert p["agreement"] == 0.0
    assert p["verdict"] == "REDUNDANT"


def test_different_families_are_never_correlated_as_one_claim():
    """SHORT_VOL and BEARISH on the same days share a trigger, not a view."""
    a = act(EVERY_THIRD_SHORT)
    b = act({i: "BEARISH" for i in EVERY_THIRD_SHORT})
    p = compare_pair(a, b)
    assert p["same_family"] is False
    assert p["signed_corr"] is None and p["agreement"] is None
    assert p["verdict"] == "CO_FIRING"
    assert p["lift"] == 3.0          # each fires 1/3 of sessions, always together


def test_lift_is_one_under_independence():
    a = act({i: "BULLISH" for i in range(0, 30, 2)})       # 15 of 30
    b = act({i: "BULLISH" for i in range(0, 30, 3)})       # 10 of 30, 5 joint
    p = compare_pair(a, b)
    assert p["joint_fires"] == 5
    assert p["lift"] == 1.0


def test_too_few_joint_fires_is_insufficient_not_distinct():
    a = act({0: "SHORT_VOL", 1: "SHORT_VOL"})
    b = act({0: "SHORT_VOL", 1: "SHORT_VOL"})
    assert compare_pair(a, b)["verdict"] == "INSUFFICIENT_DATA"


def test_only_sessions_both_signals_saw_are_compared():
    """
    A session one signal was never evaluated on is not a session it was silent
    on; counting it would dilute every rate.
    """
    a = act(EVERY_THIRD_SHORT)
    b = {d: v for d, v in act(EVERY_THIRD_SHORT).items() if d < "2026-03-17"}
    p = compare_pair(a, b)
    assert p["sessions"] == len(b)


# ── The report ────────────────────────────────────────────────────────────────

def test_report_orders_the_most_related_pairs_first_and_flags_silence():
    same = act(EVERY_THIRD_SHORT)
    report = correlation_report({
        "vrp": same,
        "term_structure": dict(same),
        "gex_regime": act({i: "BULLISH" for i in range(1, 30, 2)}),
        "never": act({}),
    })
    first = report["pairs"][0]
    assert {first["a"], first["b"]} == {"term_structure", "vrp"}
    assert first["verdict"] == "REDUNDANT"
    assert report["signals"]["never"]["fires"] == 0
    assert any("Never fired" in n for n in report["notes"])
    assert any("count each such group once" in n for n in report["notes"])


# ── Through the backtester's own replay ───────────────────────────────────────

def _register_probe(sid: str, fire_on_day_parity: int, direction: Direction):
    if f"{sid}.v1" in _REGISTRY:
        return

    @register_signal(sid, version=1, min_history=0)
    def _probe(ctx):
        day = int(ctx.snapshot.session_date[-2:])
        if day % 2 != fire_on_day_parity:
            return SignalResult.skip(sid, 1, ctx, "off day")
        return SignalResult.fire(Signal(
            signal_id=sid, version=1, ts=ctx.ts, symbol=ctx.symbol,
            direction=direction, strength=0.5))


_register_probe("corr_probe_even_a", 0, Direction.SHORT_VOL)
_register_probe("corr_probe_even_b", 0, Direction.SHORT_VOL)
_register_probe("corr_probe_odd", 1, Direction.LONG_VOL)


def _eod_store(tmp: str, dates: list[str]) -> str:
    from recorder.store import init_db, write_snapshot

    db = os.path.join(tmp, "eod.db")
    init_db(db)
    for d in dates:
        write_snapshot(ChainSnapshot(
            ts=f"{d}T15:30:00+05:30", session_date=d,
            session_phase=SessionPhase.END_OF_DAY, symbol="NIFTY",
            expiry_date="27-10-2026", expiry_epoch=1, spot=24000.0,
            rows=(ChainRow(strike=24000.0, option_type="CE", ltp=100.0),)), db)
    return db


def test_session_activity_matches_what_the_replay_fires():
    dates = [d for d in DATES
             if dt.date.fromisoformat(d).weekday() < 5]
    with tempfile.TemporaryDirectory() as tmp:
        db = _eod_store(tmp, dates)
        acts = {sid: session_activity(sid, "NIFTY", dates, db_path=db)
                for sid in ("corr_probe_even_a", "corr_probe_even_b", "corr_probe_odd")}

    assert set(acts["corr_probe_even_a"]) == set(dates)     # every session evaluated
    for d, v in acts["corr_probe_even_a"].items():
        assert v == ("SHORT_VOL" if int(d[-2:]) % 2 == 0 else None)

    report = correlation_report(acts)
    verdicts = {frozenset((p["a"], p["b"])): p for p in report["pairs"]}
    twin = verdicts[frozenset(("corr_probe_even_a", "corr_probe_even_b"))]
    assert twin["verdict"] == "REDUNDANT"
    # Never fire on the same session: joint fires zero, so nothing to say.
    apart = verdicts[frozenset(("corr_probe_even_a", "corr_probe_odd"))]
    assert apart["joint_fires"] == 0
    assert apart["verdict"] == "INSUFFICIENT_DATA"
