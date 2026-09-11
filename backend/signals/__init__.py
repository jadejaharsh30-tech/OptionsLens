# optionslens/backend/signals/
"""
Signal framework.

The governing constraint: **a signal must run identically live and in backtest**.
That is enforced structurally rather than by convention — a signal receives a
`SignalContext` built from `ChainSnapshot` objects and nothing else. There is no
Fyers client to reach for, so a signal that "works live but not in backtest"
cannot be written here.

Layout:
    base.py      Signal / SignalContext / SignalResult — the data contract
    registry.py  versioned, parameterised registration of signal functions
    features.py  derived quantities (IV, Greeks, GEX, skew, VRP) from raw rows
    store.py     evaluation log — every evaluation persisted, not just fires
"""
from signals.base import Direction, Signal, SignalContext, SignalResult  # noqa: F401
from signals.registry import (  # noqa: F401
    SignalSpec, get_signal, list_signals, register_signal,
)
