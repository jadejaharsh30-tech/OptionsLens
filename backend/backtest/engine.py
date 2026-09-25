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
    BenchmarkComparison, build_daily_null_labels, build_null_labels, compare,
    sample_null_entries, sample_null_sessions,
)
from backtest.labels import (
    ForwardReturns, compute_daily_forward_returns, compute_forward_returns,
    daily_horizon_keys, horizon_keys,
)
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
    label_mode:    str = "intraday"
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
            "label_mode":    self.label_mode,
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
    label_mode: str = "auto",
    extras: Optional[dict[str, Any]] = None,
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
    fired_entries: list[tuple[str, str, int]] = []   # (session_date, ts, direction)

    for replay in replay_range(spec, symbol, dates, params, history_window,
                               db_path=db_path, extras=extras):
        if not replay.timestamps:
            continue
        session_series[replay.session_date] = (replay.timestamps, replay.spots)
        all_results.extend(replay.results)

        for res in replay.fired:
            direction = -1 if (res.signal and res.signal.direction in _SHORT_DIRECTIONS) else 1
            fired_entries.append((replay.session_date, res.ts, direction))

    if persist_evaluations and all_results:
        write_evaluations(all_results, run_id=run_id, params=params or {},
                          **kwargs)

    # ── Choose the labelling frequency ───────────────────────────────────────
    # Exchange EOD history carries one bar per session, so intraday horizons
    # would all score n=0 and the EOD label would compare a bar with itself.
    # Detected from the data rather than configured, so a caller cannot score
    # daily bars on intraday horizons by forgetting a flag.
    if label_mode == "auto":
        one_bar_sessions = all(len(ts) == 1 for ts, _ in session_series.values())
        resolved_mode = "daily" if (one_bar_sessions and session_series) else "intraday"
    else:
        resolved_mode = label_mode

    run = BacktestRun(
        run_id        = run_id,
        signal_id     = spec.signal_id,
        version       = spec.version,
        symbol        = symbol,
        params        = {**spec.default_params, **(params or {})},
        session_dates = sorted(session_series.keys()),
        n_evaluations = len(all_results),
        n_signals     = len(fired_entries),
        generated_at  = datetime.now(IST).isoformat(),
        label_mode    = resolved_mode,
    )

    if not fired_entries:
        run.notes.append(
            "No signals fired over the available data — nothing to evaluate. "
            "Check the evaluation log's skip reasons to see whether the signal "
            "is being rejected by its threshold or never running at all."
        )
        return run

    if resolved_mode == "daily":
        signal_labels, null_labels, horizons = _label_daily(
            fired_entries, session_series, symbol,
            null_samples_per_signal, seed,
        )
    else:
        signal_labels, null_labels, horizons = _label_intraday(
            fired_entries, session_series, symbol,
            null_samples_per_signal, seed,
        )

    run.n_signals = len(signal_labels)
    if not signal_labels:
        run.notes.append("Signals fired but none could be labelled.")
        return run

    run.comparison = compare(signal_labels, null_labels, horizons)

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
            for h in horizons
        },
    )
    stats.notes = interpret(stats)
    run.stats = stats
    run.notes.extend(stats.notes)

    if resolved_mode == "daily":
        run.notes.append(
            "Daily labelling: horizons count trading sessions, and the null is "
            "matched on weekday so the weekly expiry cycle is controlled for."
        )

    if len(run.session_dates) < 20:
        run.notes.append(
            f"Only {len(run.session_dates)} sessions of recorded data. Treat "
            f"every number here as provisional — this is a plumbing check, not "
            f"evidence of edge."
        )

    return run


# ── Labelling strategies ─────────────────────────────────────────────────────

def _label_intraday(fired_entries, session_series, symbol,
                    null_samples_per_signal, seed):
    """Within-session horizons, null matched on clock time."""
    signal_labels: list[ForwardReturns] = []
    direction_by_ts: dict[str, int] = {}

    for session_date, ts, direction in fired_entries:
        timestamps, spots = session_series[session_date]
        try:
            idx = timestamps.index(ts)
        except ValueError:
            continue
        direction_by_ts[ts] = direction
        signal_labels.append(compute_forward_returns(
            index=idx, timestamps=timestamps, spots=spots,
            symbol=symbol, direction=direction,
        ))

    picks = sample_null_entries(
        signal_timestamps  = [lab.ts for lab in signal_labels],
        session_series     = session_series,
        samples_per_signal = null_samples_per_signal,
        seed               = seed,
    )
    null_labels = build_null_labels(picks, session_series, symbol, direction_by_ts)
    return signal_labels, null_labels, horizon_keys()


def _label_daily(fired_entries, session_series, symbol,
                 null_samples_per_signal, seed):
    """
    Cross-session horizons, null matched on weekday.

    The session series is one close per date — the last spot of each session,
    which for an EOD bar is the only spot it has.
    """
    dates = sorted(session_series)
    closes = [session_series[d][1][-1] for d in dates]
    index_of_date = {d: i for i, d in enumerate(dates)}

    signal_labels: list[ForwardReturns] = []
    directions: list[int] = []

    for session_date, _ts, direction in fired_entries:
        idx = index_of_date.get(session_date)
        if idx is None:
            continue
        directions.append(direction)
        signal_labels.append(compute_daily_forward_returns(
            index=idx, dates=dates, closes=closes,
            symbol=symbol, direction=direction,
        ))

    signal_indices = [index_of_date[d] for d, _, _ in fired_entries
                      if d in index_of_date]
    picks = sample_null_sessions(
        signal_indices     = signal_indices,
        dates              = dates,
        samples_per_signal = null_samples_per_signal,
        seed               = seed,
    )
    # Mirror the signal's direction mix onto the null, repeated per sample.
    null_directions = [d for d in directions for _ in range(null_samples_per_signal)]
    null_labels = build_daily_null_labels(picks, dates, closes, symbol,
                                          null_directions)
    return signal_labels, null_labels, daily_horizon_keys()
