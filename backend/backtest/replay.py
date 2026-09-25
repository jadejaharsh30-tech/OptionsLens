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
    # The window as it stood after the last bar, so `replay_range` can carry it
    # into the next session without re-reading anything.
    end_history:  tuple[ChainSnapshot, ...] = ()

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
    seed_history: tuple[ChainSnapshot, ...] = (),
) -> ReplayResult:
    """
    Replay one symbol-day, evaluating `spec` at every snapshot.

    `history_window` bounds what a signal can see, mirroring the live runner's
    memory. Making it unbounded would let a backtest use more context than the
    live system ever has, which quietly inflates results.
    """
    kwargs = {"db_path": db_path} if db_path else {}
    snapshots = list(iter_snapshots(symbol, session_date, **kwargs))

    # Seeded from prior sessions when the caller carries it. Still appended to
    # only AFTER evaluation, so a bar never sees itself or anything later.
    history: deque[ChainSnapshot] = deque(seed_history, maxlen=history_window)
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
        end_history=tuple(history),
    )


def replay_range(
    spec: SignalSpec,
    symbol: str,
    session_dates: Optional[list[str]] = None,
    params: Optional[dict[str, Any]] = None,
    history_window: int = 60,
    db_path: Optional[str] = None,
    extras: Optional[dict[str, Any]] = None,
    carry_history: bool = False,
) -> Iterator[ReplayResult]:
    """
    Replay many sessions, oldest first.

    `carry_history` decides whether a session starts with the tail of the
    previous one. It must be True for one-bar-per-session data, where otherwise
    the window is always empty and any `min_history` above zero skips forever.

    It must be False for intraday bars: carrying yesterday's last few minutes
    into today's open would let an OI-velocity rule match a "spike" across the
    overnight gap, where open interest has been restated against a new
    settlement. `run_backtest` decides from the data rather than guessing.
    """
    kwargs = {"db_path": db_path} if db_path else {}
    dates = session_dates or recorded_dates(symbol, **kwargs)
    carried: tuple[ChainSnapshot, ...] = ()
    for d in sorted(dates):
        result = replay_session(spec, symbol, d, params, history_window,
                                extras=extras, db_path=db_path,
                                seed_history=carried)
        if carry_history:
            carried = result.end_history
        yield result
