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
