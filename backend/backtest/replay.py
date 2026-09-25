# optionslens/backend/backtest/replay.py
"""
Snapshot replay.

Feeds recorded ChainSnapshots to a signal in the same shape the live runner
does. The rolling history window is built strictly from snapshots at or before
the current one, which is what makes look-ahead structurally impossible rather
than merely discouraged.
"""
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from recorder.models import ChainSnapshot
from recorder.store import iter_snapshots, recorded_dates
from signals.base import SignalContext, SignalResult
from signals.registry import SignalSpec


@dataclass
class ReplayResult:
    """One session replayed through one signal."""
    symbol:       str
    session_date: str
    results:      list[SignalResult]
    timestamps:   list[str]
    spots:        list[float]

    @property
    def fired(self) -> list[SignalResult]:
        return [r for r in self.results if r.fired]


def replay_session(
    spec: SignalSpec,
    symbol: str,
    session_date: str,
    params: Optional[dict[str, Any]] = None,
    history_window: int = 60,
    extras: Optional[dict[str, Any]] = None,
    db_path: Optional[str] = None,
) -> ReplayResult:
    """
    Replay one symbol-day, evaluating `spec` at every snapshot.

    `history_window` bounds what a signal can see, mirroring the live runner's
    memory. Making it unbounded would let a backtest use more context than the
    live system ever has, which quietly inflates results.
    """
    kwargs = {"db_path": db_path} if db_path else {}
    snapshots = list(iter_snapshots(symbol, session_date, **kwargs))

    history: deque[ChainSnapshot] = deque(maxlen=history_window)
    results, timestamps, spots = [], [], []

    for snap in snapshots:
        ctx = SignalContext(
            snapshot = snap,
            history  = tuple(history),     # strictly prior snapshots only
            params   = params or {},
            extras   = extras or {},
        )
        results.append(spec.evaluate(ctx, params))
        timestamps.append(snap.ts)
        spots.append(snap.spot)
        history.append(snap)               # appended AFTER evaluation

    return ReplayResult(
        symbol=symbol, session_date=session_date,
        results=results, timestamps=timestamps, spots=spots,
    )


def replay_range(
    spec: SignalSpec,
    symbol: str,
    session_dates: Optional[list[str]] = None,
    params: Optional[dict[str, Any]] = None,
    history_window: int = 60,
    db_path: Optional[str] = None,
    extras: Optional[dict[str, Any]] = None,
) -> Iterator[ReplayResult]:
    """Replay many sessions, oldest first."""
    kwargs = {"db_path": db_path} if db_path else {}
    dates = session_dates or recorded_dates(symbol, **kwargs)
    for d in sorted(dates):
        yield replay_session(spec, symbol, d, params, history_window,
                             extras=extras, db_path=db_path)
