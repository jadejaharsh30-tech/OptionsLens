# optionslens/backend/backtest/significance.py
"""
Is an edge real, when the observations behind it are not independent?

THE PROBLEM THIS SOLVES
    A signal ranked on a slow-moving series (VRP, implied correlation) fires in
    long runs of consecutive sessions, and a 20-session outcome started on one
    day shares 19 of its 20 days with the outcome started the next. Two hundred
    fires can carry about a dozen independent observations. A t-test that
    counts them as two hundred reports a standard error several times too small.

    Measured, not argued: on 1,000 simulated markets where the signal had NO
    edge by construction (549 sessions, a persistent percentile signal,
    overlapping 20-session outcomes), the Welch t-test this backtester used
    said EDGE in 35.5% of them, against the 5% it claims. The first real runs
    on NIFTY reported two EDGE verdicts through it.

THE TEST
    Circular shift. Keep the outcome series exactly as it happened; take the
    signal's own firing pattern — its runs, its gaps, its direction mix — and
    slide the whole thing to another point in time, wrapping around the end.
    Recompute the edge. Do that for every offset long enough that no shifted
    outcome overlaps an original one. The p-value is the share of shifted
    placements that did at least as well as the real one.

    The null is therefore "this exact pattern of bets, placed at a random
    time". It keeps both things the t-test throws away — the clustering of the
    fires and the overlap of the outcomes — so neither can manufacture
    significance. On the same simulation it said EDGE in 7% of no-edge worlds
    (within sampling error of 5%), and detected a real edge of half a standard
    deviation 54% of the time. Alternatives measured and rejected: Newey-West
    standard errors, 14% (the persistent regressor defeats its small-sample
    correction); means of non-overlapping blocks, 0% (no power at all).

    What it still assumes: that the outcome series is stationary over the
    sample. A signal that fires mostly in one half of a sample whose second
    half had systematically different outcomes can still look significant.
    Two years of one market is one regime history; the test cannot fix that.

THE STATISTIC
    edge = mean over fired sessions of  sign · (outcome − mean outcome)

    where the mean outcome is taken over every session with an outcome, and
    sign is +1/−1 by the fire's direction. That is the signal's average outcome
    minus what the same direction mix would have earned on an average day —
    the same quantity the matched-null column estimates, computed against every
    session rather than a random draw of them.

Pure functions over plain lists. No database, no randomness: every eligible
offset is used, so the p-value is reproducible without a seed.
"""
from dataclasses import dataclass
from typing import Optional

ALPHA = 0.05

# With k offsets the smallest attainable p is 1/(k+1). Below 19 offsets the
# test cannot reach 0.05 at all, so a "no edge" from it would be meaningless.
MIN_OFFSETS = 19


@dataclass
class ShiftTest:
    edge:      Optional[float]   # same unit as the outcomes (bps or vol points)
    p_value:   Optional[float]
    n_fires:   int               # fired sessions that had an outcome
    n_offsets: int

    @property
    def usable(self) -> bool:
        return self.p_value is not None


def edge_statistic(signs: list[int], outcomes: list[Optional[float]],
                   mean_outcome: Optional[float] = None) -> Optional[float]:
    """
    Mean over fired sessions of sign·(outcome − mean outcome).

    Sessions without an outcome (too close to the end of data, no entry IV)
    drop out of both the fires and the mean.
    """
    if mean_outcome is None:
        vals = [v for v in outcomes if v is not None]
        if not vals:
            return None
        mean_outcome = sum(vals) / len(vals)
    num, den = 0.0, 0
    for s, v in zip(signs, outcomes):
        if s and v is not None:
            num += s * (v - mean_outcome)
            den += 1
    return num / den if den else None


def circular_shift_test(signs: list[int], outcomes: list[Optional[float]],
                        min_offset: int) -> ShiftTest:
    """
    Two-sided circular-shift test of the edge.

    Args:
        signs:      per session, +1 / −1 for a fire in that direction, 0 for none
        outcomes:   per session, the outcome a +1 position would have had, or
                    None where it cannot be measured
        min_offset: the horizon in sessions; shifts shorter than this would let
                    a shifted outcome overlap an original one, and the test
                    would partly compare the signal with itself
    """
    n = len(signs)
    if n != len(outcomes):
        raise ValueError("signs and outcomes must cover the same sessions")

    vals = [v for v in outcomes if v is not None]
    n_fires = sum(1 for s, v in zip(signs, outcomes) if s and v is not None)
    if not vals or not n_fires:
        return ShiftTest(None, None, n_fires, 0)

    mean_outcome = sum(vals) / len(vals)
    observed = edge_statistic(signs, outcomes, mean_outcome)

    offsets = range(max(1, min_offset), n - max(1, min_offset) + 1)
    if len(offsets) < MIN_OFFSETS or observed is None:
        return ShiftTest(observed, None, n_fires, len(offsets))

    target = abs(observed) - 1e-12
    as_extreme = counted = 0
    for k in offsets:
        shifted = signs[-k:] + signs[:-k]
        stat = edge_statistic(shifted, outcomes, mean_outcome)
        if stat is None:
            continue
        counted += 1
        if abs(stat) >= target:
            as_extreme += 1

    if counted < MIN_OFFSETS:
        return ShiftTest(observed, None, n_fires, counted)
    return ShiftTest(round(observed, 4), round((as_extreme + 1) / (counted + 1), 4),
                     n_fires, counted)


def horizon_sessions(key: str) -> Optional[int]:
    """'20d' or '20d_vol' -> 20. None for intraday keys ('15m', 'eod')."""
    head = key.split("_")[0]
    if head.endswith("d") and head[:-1].isdigit():
        return int(head[:-1])
    return None


# ── Intraday: shift whole sessions, keep the clock ───────────────────────────
#
# Intraday horizons (5m-60m, eod) never cross a session boundary, so the
# overlap lives INSIDE a session: a signal that fires on 40 consecutive
# minutes contributes 40 end-of-day outcomes that share almost every bar.
# Sliding fires along the minute axis would fix that but break the clock-time
# matching the intraday null exists for — open and close behave differently,
# and a signal that fires mostly at 09:20 must be compared with 09:20s.
#
# So the shift moves each session's ENTIRE firing pattern, at the same clock
# times, onto another session. Within-session clustering, outcome overlap and
# time of day are all preserved; only the session the bets landed on changes.
# Measured on simulated sessions with no edge, a morning drift and a signal
# that fires more in the morning: Welch t said EDGE 37.3% (15m) and 60.0%
# (eod); this test 5.3% and 6.7%. A real 1 bp-per-fire edge over 40 sessions
# was detected 85% of the time.
#
# Outcomes are centred on the mean for their CLOCK TIME across sessions, so a
# signal that fires in the drifting morning gets no credit for the drift.

# Cap on offsets: p resolution of 1/(k+1) is ample at a few hundred, and the
# cost grows with fires x offsets.
MAX_SESSION_OFFSETS = 199


def session_shift_test(fires: list[tuple[int, str, int]],
                       outcomes: dict[tuple[int, str], Optional[float]],
                       n_sessions: int) -> ShiftTest:
    """
    Two-sided test of an intraday edge by shifting whole sessions.

    Args:
        fires:      (session index, "HH:MM", sign) for every fire
        outcomes:   (session index, "HH:MM") -> outcome of a +1 position
                    entered then, or None where the horizon runs past the close
        n_sessions: sessions in the sample, indexed 0..n-1
    """
    by_clock: dict[str, list[float]] = {}
    for (_, clock), v in outcomes.items():
        if v is not None:
            by_clock.setdefault(clock, []).append(v)
    mean_at = {c: sum(v) / len(v) for c, v in by_clock.items()}

    def stat(k: int) -> Optional[float]:
        num, den = 0.0, 0
        for s, clock, sign in fires:
            v = outcomes.get(((s + k) % n_sessions, clock))
            m = mean_at.get(clock)
            if v is None or m is None:
                continue
            num += sign * (v - m)
            den += 1
        return num / den if den else None

    observed = stat(0)
    n_fires = sum(1 for s, c, _ in fires if outcomes.get((s, c)) is not None)
    if observed is None:
        return ShiftTest(None, None, n_fires, 0)

    all_offsets = list(range(1, n_sessions))
    if len(all_offsets) > MAX_SESSION_OFFSETS:
        step = len(all_offsets) / MAX_SESSION_OFFSETS
        all_offsets = [all_offsets[int(i * step)] for i in range(MAX_SESSION_OFFSETS)]

    target = abs(observed) - 1e-12
    as_extreme = counted = 0
    for k in all_offsets:
        st = stat(k)
        if st is None:
            continue
        counted += 1
        as_extreme += abs(st) >= target

    if counted < MIN_OFFSETS:
        return ShiftTest(round(observed, 4), None, n_fires, counted)
    return ShiftTest(round(observed, 4), round((as_extreme + 1) / (counted + 1), 4),
                     n_fires, counted)


def intraday_outcomes(timestamps: list[str], spots: list[float],
                      horizon_minutes: dict[str, Optional[int]],
                      ) -> dict[str, list[Optional[float]]]:
    """
    Forward return of a +1 position from EVERY bar of one session, per horizon.

    Same definition as `labels.compute_forward_returns` (end value is the last
    bar at or before entry + h minutes; None when the session ends first; eod
    is the session's last bar), computed in one pass with timestamps parsed
    once — the labeller parses per bar, which is fine for a few hundred fires
    and far too slow for every bar of every session.

    `horizon_minutes` maps label keys to minutes, None meaning end of session.
    """
    import bisect
    from datetime import datetime

    from backtest.labels import _bps

    times = [datetime.fromisoformat(t).timestamp() for t in timestamps]
    out: dict[str, list[Optional[float]]] = {}
    for key, minutes in horizon_minutes.items():
        vals: list[Optional[float]] = []
        for i, entry in enumerate(spots):
            if minutes is None:
                vals.append(round(_bps(entry, spots[-1]), 2))
                continue
            target = times[i] + minutes * 60
            if times[-1] < target:
                vals.append(None)
                continue
            j = bisect.bisect_right(times, target) - 1
            vals.append(round(_bps(entry, spots[j]), 2) if j > i else None)
        out[key] = vals
    return out
