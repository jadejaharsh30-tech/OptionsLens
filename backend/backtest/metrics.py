# optionslens/backend/backtest/metrics.py
"""
Performance statistics for a set of labelled signals.

Reported per horizon, never aggregated into a single headline number. A signal
that works at 5 minutes and fails at 60 is telling you something specific about
its decay, and a blended figure would hide it.

`n` accompanies every statistic on purpose. A 70% hit rate over 9 trades is
noise; the sample size is not a footnote, it is the main thing to look at first.
"""
import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HorizonStats:
    horizon:        str
    n:              int
    hit_rate_pct:   Optional[float] = None
    mean_bps:       Optional[float] = None
    median_bps:     Optional[float] = None
    stdev_bps:      Optional[float] = None
    # Mean return divided by its standard error — how many standard errors the
    # mean sits from zero. Roughly |t| > 2 is the usual "not obviously noise"
    # bar, and it is a far more honest headline than an annualised Sharpe
    # extrapolated from a handful of intraday observations.
    t_stat:         Optional[float] = None
    sharpe_per_obs: Optional[float] = None
    mean_mae_bps:   Optional[float] = None
    mean_mfe_bps:   Optional[float] = None
    best_bps:       Optional[float] = None
    worst_bps:      Optional[float] = None
    max_drawdown_bps: Optional[float] = None


@dataclass
class BacktestStats:
    signal_id:  str
    version:    int
    n_signals:  int
    by_horizon: dict[str, HorizonStats] = field(default_factory=dict)
    notes:      list[str] = field(default_factory=list)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _max_drawdown(returns: list[float]) -> float:
    """Worst peak-to-trough of the cumulative return path, in bps."""
    cum, peak, worst = 0.0, 0.0, 0.0
    for r in returns:
        cum += r
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return worst


def horizon_stats(horizon: str, returns: list[Optional[float]],
                  maes: Optional[list[Optional[float]]] = None,
                  mfes: Optional[list[Optional[float]]] = None) -> HorizonStats:
    """Statistics for one horizon. Unlabelled observations are dropped, not zeroed."""
    clean = [r for r in returns if r is not None]
    if not clean:
        return HorizonStats(horizon=horizon, n=0)

    mean  = _mean(clean)
    stdev = _stdev(clean)
    n     = len(clean)

    t_stat = None
    sharpe = None
    if stdev > 0 and n > 1:
        t_stat = round(mean / (stdev / math.sqrt(n)), 3)
        sharpe = round(mean / stdev, 4)

    clean_mae = [m for m in (maes or []) if m is not None]
    clean_mfe = [m for m in (mfes or []) if m is not None]

    return HorizonStats(
        horizon          = horizon,
        n                = n,
        hit_rate_pct     = round(sum(1 for r in clean if r > 0) / n * 100, 2),
        mean_bps         = round(mean, 3),
        median_bps       = round(_median(clean), 3),
        stdev_bps        = round(stdev, 3),
        t_stat           = t_stat,
        sharpe_per_obs   = sharpe,
        mean_mae_bps     = round(_mean(clean_mae), 2) if clean_mae else None,
        mean_mfe_bps     = round(_mean(clean_mfe), 2) if clean_mfe else None,
        best_bps         = round(max(clean), 2),
        worst_bps        = round(min(clean), 2),
        max_drawdown_bps = round(_max_drawdown(clean), 2),
    )


def interpret(stats: BacktestStats, min_sample: int = 30) -> list[str]:
    """
    Plain-language caveats attached to the result.

    Exists so a thin or insignificant result is labelled as such in the output
    itself, rather than relying on whoever reads the table to remember.
    """
    notes = []
    for h, s in stats.by_horizon.items():
        if s.n == 0:
            notes.append(f"{h}: no labelled observations.")
        elif s.n < min_sample:
            notes.append(
                f"{h}: only {s.n} observations — below the {min_sample} minimum "
                f"for any statistic here to mean much."
            )
        elif s.t_stat is not None and abs(s.t_stat) < 2.0:
            notes.append(
                f"{h}: mean {s.mean_bps} bps is {abs(s.t_stat):.2f} standard "
                f"errors from zero — not distinguishable from noise."
            )
    return notes
