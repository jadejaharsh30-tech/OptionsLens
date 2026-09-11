# optionslens/backend/signals/base.py
"""
The data contract every signal speaks.

Design notes worth keeping:

- `SignalContext` carries the current snapshot plus a bounded history window.
  Signals must not reach outside it: anything a signal can see, the backtester
  can reproduce, which is what makes a backtest result meaningful.

- A signal returns `SignalResult` even when it does NOT fire. Recording only the
  fires means you only ever study survivors, and the denominator you need to
  judge a hit rate is exactly the set of non-fires. `fired=False` results carry
  the features that were evaluated, so a rejected setup can be inspected later.

- `strength` is a continuous score in [0, 1], separate from the binary fire.
  Thresholds are parameters, not truths; keeping the score lets the backtester
  sweep the threshold after the fact instead of re-running the signal.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from recorder.models import ChainSnapshot


class Direction(str, Enum):
    """Trade direction a signal implies. NEUTRAL is for vol signals."""
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    LONG_VOL  = "LONG_VOL"
    SHORT_VOL = "SHORT_VOL"


@dataclass
class SignalContext:
    """
    Everything a signal is allowed to see.

    Args:
        snapshot: the current chain snapshot being evaluated
        history:  prior snapshots for this symbol, oldest first, bounded window
        params:   the signal's parameters for this run (see SignalSpec)
        extras:   cross-sectional data a signal may need — e.g. other symbols'
                  snapshots for dispersion, or a daily IV series for VRP
    """
    snapshot: ChainSnapshot
    history:  tuple[ChainSnapshot, ...] = ()
    params:   dict[str, Any] = field(default_factory=dict)
    extras:   dict[str, Any] = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        return self.snapshot.symbol

    @property
    def ts(self) -> str:
        return self.snapshot.ts

    def param(self, name: str, default: Any = None) -> Any:
        return self.params.get(name, default)

    def lookback(self, n: int) -> tuple[ChainSnapshot, ...]:
        """The most recent n historical snapshots, oldest first."""
        return self.history[-n:] if n > 0 else ()

    def minutes_of_history(self) -> int:
        return len(self.history)


@dataclass
class Signal:
    """A fired signal — the thing a trade proposal is built from."""
    signal_id:   str
    version:     int
    ts:          str
    symbol:      str
    direction:   Direction
    strength:    float                      # continuous score in [0, 1]
    features:    dict[str, Any] = field(default_factory=dict)
    notes:       Optional[str]  = None

    def __post_init__(self):
        self.strength = max(0.0, min(1.0, float(self.strength)))


@dataclass
class SignalResult:
    """
    The outcome of one evaluation, fired or not.

    `reason` on a non-fire is what makes the evaluation log useful: "IV rank
    below threshold" and "not enough history" are very different kinds of
    silence, and only one of them is a signal telling you something.
    """
    signal_id: str
    version:   int
    ts:        str
    symbol:    str
    fired:     bool
    signal:    Optional[Signal] = None
    features:  dict[str, Any] = field(default_factory=dict)
    reason:    Optional[str]  = None

    @classmethod
    def fire(cls, signal: Signal, reason: Optional[str] = None) -> "SignalResult":
        return cls(
            signal_id = signal.signal_id,
            version   = signal.version,
            ts        = signal.ts,
            symbol    = signal.symbol,
            fired     = True,
            signal    = signal,
            features  = signal.features,
            reason    = reason,
        )

    @classmethod
    def skip(cls, spec_id: str, version: int, ctx: SignalContext,
             reason: str, features: Optional[dict] = None) -> "SignalResult":
        return cls(
            signal_id = spec_id,
            version   = version,
            ts        = ctx.ts,
            symbol    = ctx.symbol,
            fired     = False,
            features  = features or {},
            reason    = reason,
        )
