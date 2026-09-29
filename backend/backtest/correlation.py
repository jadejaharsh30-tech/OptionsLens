# optionslens/backend/backtest/correlation.py
"""
Signal correlation report — the prerequisite for any ensemble (roadmap item 36).

WHY THIS COMES BEFORE AN ENSEMBLE
    Three signals agreeing reads as strong confirmation. It is only
    confirmation if they are measuring different things. `vrp`,
    `term_structure` and `dispersion` are all driven by the same calm/stress
    regime — calm means a positive premium, a steep curve and rich index vol
    relative to members, all at once — so on most days they will agree because
    they are the same observation three times. An ensemble that adds their
    votes counts the regime three times and reports three times the
    confidence it has earned.

    So before any combining rule is written, this measures how often the
    registered signals fire on the same sessions and, when they do, whether
    they point the same way. An ensemble is a later, separate decision; its
    inputs should be the signals this report shows to be distinct.

RESOLUTION: ONE READING PER SESSION
    Signals are replayed exactly as the backtester replays them (same history
    carry rule, same extras), then collapsed to one entry per session: the
    direction of the first fire, or nothing. Session resolution is what matters
    for the question being asked — two signals firing at 10:02 and 14:47 on the
    same day are, for a trader deciding what to hold overnight, the same day's
    trade.

WHAT THE WORDS MEAN
    Like the backtest verdicts, each pair gets a word rather than a score,
    because a correlation coefficient invites a threshold argument. These are
    screening flags, not tests; the thresholds are stated in the constants below
    and fixed in advance.

        INSUFFICIENT_DATA   too few joint fires to say anything
        REDUNDANT           same family, and the signed series move together
                            (or exactly against each other): count them once
        CO_FIRING           fire together far more often than chance, though
                            not always in the same direction — they share a
                            trigger, and an ensemble should know it
        DISTINCT            neither of the above on this data
"""
import math
from itertools import combinations
from typing import Any, Optional

from backtest.vol_labels import VOL_DIRECTIONS

# Screening thresholds. Fixed in advance and deliberately round: they flag
# pairs for a human to look at, they do not certify independence.
MIN_JOINT_FIRES = 5
REDUNDANT_ABS_CORR = 0.5
CO_FIRING_LIFT = 2.0

_SIGN = {"BULLISH": 1, "LONG_VOL": 1, "BEARISH": -1, "SHORT_VOL": -1}


def session_activity(signal_id: str, symbol: str, dates: list[str],
                     extras: Optional[dict[str, Any]] = None,
                     params: Optional[dict[str, Any]] = None,
                     history_window: int = 60,
                     db_path: Optional[str] = None) -> dict[str, Optional[str]]:
    """
    {session_date: direction of the first fire, or None} for every session the
    signal was evaluated on.

    Replays through `replay_range` with the backtester's own carry rule, so the
    fires counted here are exactly the fires a backtest of the same inputs
    would label. Sessions with no bars are absent, not None: "not evaluated"
    and "evaluated and silent" are different, and only the second is a
    denominator.
    """
    from backtest.engine import should_carry_history
    from backtest.replay import replay_range
    from signals.registry import get_signal

    spec = get_signal(signal_id)
    dates = sorted(dates)
    carry = should_carry_history(symbol, dates, db_path)

    out: dict[str, Optional[str]] = {}
    for replay in replay_range(spec, symbol, dates, params, history_window,
                               db_path=db_path, extras=extras,
                               carry_history=carry):
        if not replay.timestamps:
            continue
        first = next((r for r in replay.results if r.fired and r.signal), None)
        out[replay.session_date] = first.signal.direction.value if first else None
    return out


def family(activity: dict[str, Optional[str]]) -> Optional[str]:
    """'vol', 'directional', 'mixed', or None when the signal never fired."""
    fired = {d for d in activity.values() if d}
    if not fired:
        return None
    if fired <= VOL_DIRECTIONS:
        return "vol"
    if not fired & VOL_DIRECTIONS:
        return "directional"
    return "mixed"


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def compare_pair(a: dict[str, Optional[str]],
                 b: dict[str, Optional[str]]) -> dict:
    """
    Overlap and agreement of two signals over the sessions BOTH were evaluated on.

    `lift` is P(both fire) / (P(a) · P(b)): 1 is what independence predicts,
    above it they share triggers. `signed_corr` correlates the +1/−1/0 series,
    and is only computed within a family — SHORT_VOL and BEARISH are not the
    same claim, so a number relating them would be arithmetic without meaning.
    `agreement` is the share of joint fires pointing the same way.
    """
    common = sorted(set(a) & set(b))
    n = len(common)
    fa = sum(1 for d in common if a[d])
    fb = sum(1 for d in common if b[d])
    joint = [d for d in common if a[d] and b[d]]
    co = len(joint)

    fam_a, fam_b = family({d: a[d] for d in common}), family({d: b[d] for d in common})
    same_family = fam_a is not None and fam_a == fam_b and fam_a != "mixed"

    out: dict[str, Any] = {
        "sessions": n, "fires_a": fa, "fires_b": fb, "joint_fires": co,
        "family_a": fam_a, "family_b": fam_b, "same_family": same_family,
        "jaccard": round(co / (fa + fb - co), 4) if (fa + fb - co) else None,
        "lift": round((co / n) / ((fa / n) * (fb / n)), 3) if (n and fa and fb) else None,
        "agreement": None, "signed_corr": None,
    }
    if same_family and co:
        out["agreement"] = round(
            sum(1 for d in joint if a[d] == b[d]) / co, 4)
        corr = _pearson([_SIGN.get(a[d] or "", 0) for d in common],
                        [_SIGN.get(b[d] or "", 0) for d in common])
        out["signed_corr"] = None if corr is None else round(corr, 4)
    out["verdict"] = _verdict(out)
    return out


def _verdict(p: dict) -> str:
    if p["joint_fires"] < MIN_JOINT_FIRES:
        return "INSUFFICIENT_DATA"
    if p["signed_corr"] is not None and abs(p["signed_corr"]) >= REDUNDANT_ABS_CORR:
        return "REDUNDANT"
    if p["lift"] is not None and p["lift"] >= CO_FIRING_LIFT:
        return "CO_FIRING"
    return "DISTINCT"


def correlation_report(activities: dict[str, dict[str, Optional[str]]]) -> dict:
    """
    Per-signal fire rates plus every pairwise comparison, most related first.
    """
    signals = {}
    for sid, act in activities.items():
        fires = sum(1 for v in act.values() if v)
        signals[sid] = {
            "sessions": len(act), "fires": fires, "family": family(act),
            "fire_rate_pct": round(fires / len(act) * 100, 2) if act else 0.0,
        }

    pairs = []
    for x, y in combinations(sorted(activities), 2):
        p = compare_pair(activities[x], activities[y])
        pairs.append({"a": x, "b": y, **p})

    order = {"REDUNDANT": 0, "CO_FIRING": 1, "DISTINCT": 2, "INSUFFICIENT_DATA": 3}
    pairs.sort(key=lambda p: (order[p["verdict"]], -(p["lift"] or 0)))

    notes = []
    silent = [s for s, v in signals.items() if v["fires"] == 0]
    if silent:
        notes.append(
            f"Never fired on this data: {', '.join(silent)}. Their pairs are "
            f"INSUFFICIENT_DATA by construction — check their skip reasons "
            f"before reading that as independence.")
    if any(p["verdict"] == "REDUNDANT" for p in pairs):
        notes.append(
            "REDUNDANT pairs are one observation measured twice. An ensemble "
            "should count each such group once.")
    notes.append(
        "Screening flags, not tests: thresholds are fixed "
        f"(≥{MIN_JOINT_FIRES} joint fires, |signed corr| ≥ {REDUNDANT_ABS_CORR}, "
        f"lift ≥ {CO_FIRING_LIFT}).")
    return {"signals": signals, "pairs": pairs, "notes": notes}
