# optionslens/backend/signals/registry.py
"""
Versioned, parameterised signal registration.

Two rules this enforces:

1. **Every signal is versioned.** When you change the logic you bump the
   version, and old results stay attributable to the code that produced them.
   Without this, a backtest run three weeks ago is uninterpretable — you cannot
   tell whether a result changed because the market changed or because you
   quietly edited a threshold.

2. **Parameters live in the spec, never hardcoded in the body.** The backtester
   sweeps parameters by passing different dicts; a threshold baked into an `if`
   cannot be swept, and a signal you cannot sweep is a signal you cannot show
   is robust rather than fitted.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from signals.base import SignalContext, SignalResult

SignalFn = Callable[[SignalContext], SignalResult]


@dataclass(frozen=True)
class SignalSpec:
    """Registration record for one signal."""
    signal_id:      str
    version:        int
    fn:             SignalFn
    description:    str = ""
    default_params: dict[str, Any] = field(default_factory=dict)
    # Minimum snapshots of history before the signal can be evaluated at all.
    # Evaluating below this produces a "warming up" skip rather than a result
    # computed from too little data.
    min_history:    int = 0

    @property
    def key(self) -> str:
        return f"{self.signal_id}.v{self.version}"

    def evaluate(self, ctx: SignalContext,
                 params: Optional[dict[str, Any]] = None) -> SignalResult:
        """
        Run the signal with defaults merged under any overrides.

        Guards history length here rather than in each signal body, so every
        signal reports insufficient history the same way.
        """
        merged = {**self.default_params, **(params or {})}
        ctx = SignalContext(
            snapshot = ctx.snapshot,
            history  = ctx.history,
            params   = merged,
            extras   = ctx.extras,
        )

        if len(ctx.history) < self.min_history:
            return SignalResult.skip(
                self.signal_id, self.version, ctx,
                reason=f"warming up: {len(ctx.history)}/{self.min_history} snapshots",
            )

        return self.fn(ctx)


_REGISTRY: dict[str, SignalSpec] = {}


def register_signal(signal_id: str, version: int = 1, description: str = "",
                    default_params: Optional[dict[str, Any]] = None,
                    min_history: int = 0):
    """
    Decorator registering a signal function.

        @register_signal("gex_regime", version=1,
                         default_params={"flip_buffer_pct": 0.25},
                         min_history=5)
        def gex_regime(ctx: SignalContext) -> SignalResult:
            ...
    """
    def decorator(fn: SignalFn) -> SignalFn:
        spec = SignalSpec(
            signal_id      = signal_id,
            version        = version,
            fn             = fn,
            description    = description or (fn.__doc__ or "").strip().split("\n")[0],
            default_params = default_params or {},
            min_history    = min_history,
        )
        if spec.key in _REGISTRY:
            raise ValueError(f"Signal {spec.key} is already registered.")
        _REGISTRY[spec.key] = spec
        fn.spec = spec          # type: ignore[attr-defined]
        return fn
    return decorator


def get_signal(signal_id: str, version: Optional[int] = None) -> SignalSpec:
    """Fetch a spec. Without a version, returns the highest registered one."""
    if version is not None:
        key = f"{signal_id}.v{version}"
        if key not in _REGISTRY:
            raise KeyError(f"No signal registered as {key}")
        return _REGISTRY[key]

    matches = [s for s in _REGISTRY.values() if s.signal_id == signal_id]
    if not matches:
        raise KeyError(f"No signal registered under id '{signal_id}'")
    return max(matches, key=lambda s: s.version)


def list_signals() -> list[SignalSpec]:
    return sorted(_REGISTRY.values(), key=lambda s: (s.signal_id, s.version))


def clear_registry():
    """Test helper — never call from application code."""
    _REGISTRY.clear()
