# optionslens/backend/backtest/benchmark.py
"""
Null benchmark: random entries drawn at the same times of day.

The question a backtest must answer is not "did this make money" but "did this
make more money than an uninformed trade taken at the same moment". Those come
apart constantly in intraday options:

- A signal that mostly fires in the first 15 minutes inherits opening drift.
- One that fires near expiry inherits pin behaviour.
- One that fires on high-vol days inherits that day's trend.

Matching the null on time-of-day removes the first two. Matching it on session
date removes the third. What survives is attributable to the signal's actual
discrimination rather than to when and where it chose to look.

The sampler is seeded so a reported result can be reproduced exactly.
"""
import random
from dataclasses import dataclass
from typing import Optional

from backtest.labels import ForwardReturns, compute_forward_returns, horizon_keys
from backtest.metrics import HorizonStats, horizon_stats


@dataclass
class BenchmarkComparison:
    """Signal versus null at one horizon."""
    horizon:         str
    signal:          HorizonStats
    null:            HorizonStats
    edge_bps:        Optional[float] = None     # signal mean minus null mean
    edge_t_stat:     Optional[float] = None     # Welch t on the difference
    beats_null:      bool = False

    def verdict(self) -> str:
        if self.signal.n < 30:
            return "INSUFFICIENT_DATA"
        if self.edge_t_stat is None:
            return "INDETERMINATE"
        if abs(self.edge_t_stat) < 2.0:
            return "NO_EDGE"
        return "EDGE" if self.edge_bps and self.edge_bps > 0 else "INVERSE_EDGE"


def sample_null_entries(
    signal_timestamps: list[str],
    session_series: dict[str, tuple[list[str], list[float]]],
    samples_per_signal: int = 10,
    seed: int = 42,
) -> list[tuple[str, str, int]]:
    """
    Draw random entries matched to the signals' time-of-day distribution.

    Args:
        signal_timestamps: when the signal actually fired (ISO, with date)
        session_series: session_date -> (timestamps, spots)
        samples_per_signal: draws per real signal; more shrinks the null's own
            sampling error, which otherwise dominates the comparison

    Returns (session_date, timestamp, index) triples to label like real signals.

    Matching is on clock time (HH:MM) across all available sessions: for each
    real signal we draw entries at that same minute on randomly chosen days.
    """
    rng = random.Random(seed)
    dates = sorted(session_series.keys())
    if not dates:
        return []

    # minute-of-day -> index, per session, for O(1) matching
    lookup: dict[str, dict[str, int]] = {}
    for d, (timestamps, _) in session_series.items():
        lookup[d] = {ts[11:16]: i for i, ts in enumerate(timestamps)}

    picks: list[tuple[str, str, int]] = []
    for sig_ts in signal_timestamps:
        clock = sig_ts[11:16]
        candidates = [d for d in dates if clock in lookup[d]]
        if not candidates:
            continue
        for _ in range(samples_per_signal):
            d = rng.choice(candidates)
            idx = lookup[d][clock]
            picks.append((d, session_series[d][0][idx], idx))
    return picks


def build_null_labels(
    picks: list[tuple[str, str, int]],
    session_series: dict[str, tuple[list[str], list[float]]],
    symbol: str,
    direction_by_ts: Optional[dict[str, int]] = None,
) -> list[ForwardReturns]:
    """
    Label the null entries exactly as the real signals were labelled.

    `direction_by_ts` mirrors the signal's long/short mix onto the null. Without
    it a directionally biased signal would be compared against a null that is
    implicitly always long, and in a trending sample that comparison is rigged.
    """
    labels = []
    for session_date, ts, idx in picks:
        timestamps, spots = session_series[session_date]
        direction = (direction_by_ts or {}).get(ts, 1)
        labels.append(compute_forward_returns(
            index=idx, timestamps=timestamps, spots=spots,
            symbol=symbol, direction=direction,
        ))
    return labels


def _welch_t(mean_a: float, sd_a: float, n_a: int,
             mean_b: float, sd_b: float, n_b: int) -> Optional[float]:
    """Welch's t — unequal variances and very unequal sample sizes are the norm here."""
    if n_a < 2 or n_b < 2:
        return None
    se_sq = (sd_a ** 2) / n_a + (sd_b ** 2) / n_b
    if se_sq <= 0:
        return None
    return (mean_a - mean_b) / (se_sq ** 0.5)


def compare(signal_labels: list[ForwardReturns],
            null_labels: list[ForwardReturns],
            horizons: Optional[list[str]] = None) -> dict[str, BenchmarkComparison]:
    """Signal-versus-null comparison at every horizon."""
    horizons = horizons or horizon_keys()
    out: dict[str, BenchmarkComparison] = {}

    for h in horizons:
        sig = horizon_stats(
            h,
            [lab.returns_bps.get(h) for lab in signal_labels],
            [lab.mae_bps.get(h) for lab in signal_labels],
            [lab.mfe_bps.get(h) for lab in signal_labels],
        )
        null = horizon_stats(
            h,
            [lab.returns_bps.get(h) for lab in null_labels],
        )

        edge = t = None
        if sig.n and null.n and sig.mean_bps is not None and null.mean_bps is not None:
            edge = round(sig.mean_bps - null.mean_bps, 3)
            t = _welch_t(sig.mean_bps, sig.stdev_bps or 0.0, sig.n,
                         null.mean_bps, null.stdev_bps or 0.0, null.n)
            t = round(t, 3) if t is not None else None

        cmp_ = BenchmarkComparison(
            horizon=h, signal=sig, null=null, edge_bps=edge, edge_t_stat=t,
        )
        cmp_.beats_null = cmp_.verdict() == "EDGE"
        out[h] = cmp_

    return out
