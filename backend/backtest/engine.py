# optionslens/backend/backtest/engine.py
"""
Backtest orchestration.

Pipeline:
    replay snapshots -> evaluate signal -> label forward returns
                     -> build matched null -> compare -> report

Every run gets a `run_id` and records the exact signal version and parameters
used. A result you cannot reproduce is an anecdote, and a result you cannot
attribute to a specific version of the code is worse than none at all.
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from backtest.benchmark import (
    BenchmarkComparison, build_null_labels, compare, sample_null_entries,
)
from backtest.labels import ForwardReturns, compute_forward_returns, horizon_keys
from backtest.metrics import BacktestStats, horizon_stats, interpret
from backtest.replay import replay_range
from market_hours import IST
from recorder.store import recorded_dates
from signals.base import Direction
from signals.registry import SignalSpec, get_signal
from signals.store import write_evaluations

# Directions that imply a short view of the underlying, for label sign flipping.
_SHORT_DIRECTIONS = {Direction.BEARISH}


@dataclass
class BacktestRun:
    run_id:       str
    signal_id:    str
    version:      int
    symbol:       str
    params:       dict[str, Any]
    session_dates: list[str]
    n_evaluations: int
    n_signals:     int
    stats:         Optional[BacktestStats] = None
    comparison:    dict[str, BenchmarkComparison] = field(default_factory=dict)
    notes:         list[str] = field(default_factory=list)
    generated_at:  str = ""

    def to_dict(self) -> dict:
        return {
            "run_id":        self.run_id,
            "signal_id":     self.signal_id,
            "version":       self.version,
            "symbol":        self.symbol,
            "params":        self.params,
            "sessions":      len(self.session_dates),
            "session_dates": self.session_dates,
            "evaluations":   self.n_evaluations,
            "signals_fired": self.n_signals,
            "fire_rate_pct": round(self.n_signals / self.n_evaluations * 100, 2)
                             if self.n_evaluations else 0.0,
            "generated_at":  self.generated_at,
            "notes":         self.notes,
            "horizons": {
                h: {
                    "signal": vars(c.signal),
                    "null":   vars(c.null),
                    "edge_bps":    c.edge_bps,
                    "edge_t_stat": c.edge_t_stat,
                    "verdict":     c.verdict(),
                }
                for h, c in self.comparison.items()
            },
        }


def run_backtest(
    signal_id: str,
    symbol: str,
    version: Optional[int] = None,
    params: Optional[dict[str, Any]] = None,
    session_dates: Optional[list[str]] = None,
    history_window: int = 60,
    null_samples_per_signal: int = 10,
    seed: int = 42,
    persist_evaluations: bool = True,
    db_path: Optional[str] = None,
) -> BacktestRun:
    """
    Run one signal over recorded data and compare it against a matched null.

    Returns a BacktestRun even when nothing fired — a signal that never triggers
    is a finding, and silently returning nothing would hide it.
    """
    spec: SignalSpec = get_signal(signal_id, version)
    run_id = uuid.uuid4().hex[:12]
    kwargs = {"db_path": db_path} if db_path else {}

    dates = session_dates or recorded_dates(symbol, **kwargs)
    dates = sorted(dates)

    all_results = []
    session_series: dict[str, tuple[list[str], list[float]]] = {}
    signal_labels: list[ForwardReturns] = []
    direction_by_ts: dict[str, int] = {}

    for replay in replay_range(spec, symbol, dates, params, history_window,
                               db_path=db_path):
        if not replay.timestamps:
            continue
        session_series[replay.session_date] = (replay.timestamps, replay.spots)
        all_results.extend(replay.results)

        index_of = {ts: i for i, ts in enumerate(replay.timestamps)}
        for res in replay.fired:
            idx = index_of.get(res.ts)
            if idx is None:
                continue
            direction = -1 if (res.signal and res.signal.direction in _SHORT_DIRECTIONS) else 1
            direction_by_ts[res.ts] = direction
            signal_labels.append(compute_forward_returns(
                index=idx,
                timestamps=replay.timestamps,
                spots=replay.spots,
                symbol=symbol,
                direction=direction,
            ))

    if persist_evaluations and all_results:
        write_evaluations(all_results, run_id=run_id, params=params or {},
                          **kwargs)

    run = BacktestRun(
        run_id        = run_id,
        signal_id     = spec.signal_id,
        version       = spec.version,
        symbol        = symbol,
        params        = {**spec.default_params, **(params or {})},
        session_dates = sorted(session_series.keys()),
        n_evaluations = len(all_results),
        n_signals     = len(signal_labels),
        generated_at  = datetime.now(IST).isoformat(),
    )

    if not signal_labels:
        run.notes.append(
            "No signals fired over the available data — nothing to evaluate. "
            "Check the evaluation log's skip reasons to see whether the signal "
            "is being rejected by its threshold or never running at all."
        )
        return run

    # ── Matched null ─────────────────────────────────────────────────────────
    picks = sample_null_entries(
        signal_timestamps  = [lab.ts for lab in signal_labels],
        session_series     = session_series,
        samples_per_signal = null_samples_per_signal,
        seed               = seed,
    )
    null_labels = build_null_labels(picks, session_series, symbol, direction_by_ts)

    run.comparison = compare(signal_labels, null_labels)

    stats = BacktestStats(
        signal_id = spec.signal_id,
        version   = spec.version,
        n_signals = len(signal_labels),
        by_horizon = {
            h: horizon_stats(
                h,
                [lab.returns_bps.get(h) for lab in signal_labels],
                [lab.mae_bps.get(h) for lab in signal_labels],
                [lab.mfe_bps.get(h) for lab in signal_labels],
            )
            for h in horizon_keys()
        },
    )
    stats.notes = interpret(stats)
    run.stats = stats
    run.notes.extend(stats.notes)

    if len(run.session_dates) < 20:
        run.notes.append(
            f"Only {len(run.session_dates)} sessions of recorded data. Treat "
            f"every number here as provisional — this is a plumbing check, not "
            f"evidence of edge."
        )

    return run
