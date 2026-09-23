# optionslens/backend/trading/broker.py
"""
Execution adapters.

`PaperBroker` is the default and the only implementation that actually
executes. `LiveBroker` exists to prove the abstraction holds, and deliberately
refuses to place orders: wiring real order placement is a decision that should
be made explicitly, with the user present, not inherited from a framework
commit.

Paper fills reuse the backtester's cost model rather than reimplementing it.
If paper fills were optimistic relative to the backtest, forward results would
disagree with backtest results for reasons that have nothing to do with the
signal — and you would spend weeks chasing the wrong ghost.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from backtest.costs import CostConfig, Fill, execute_leg
from trading.models import TradeLeg


@dataclass
class OrderResult:
    filled:   bool
    price:    Optional[float] = None
    charges:  float = 0.0
    reason:   str = ""
    fill:     Optional[Fill] = None


class Broker(ABC):
    """Execution interface. Paper and live must be substitutable."""

    @property
    @abstractmethod
    def is_live(self) -> bool: ...

    @abstractmethod
    def place(self, leg: TradeLeg, bid: float, ask: float, ltp: float,
              is_buy: bool) -> OrderResult: ...


class PaperBroker(Broker):
    """
    Simulated execution with the backtester's cost model.

    Fills cross the spread by default — buy the ask, sell the bid — because you
    cannot trade at mid, and a paper engine that pretends otherwise produces a
    track record that will not survive contact with a real book.
    """

    def __init__(self, cost_config: Optional[CostConfig] = None):
        self.cfg = cost_config or CostConfig()

    @property
    def is_live(self) -> bool:
        return False

    def place(self, leg: TradeLeg, bid: float, ask: float, ltp: float,
              is_buy: bool) -> OrderResult:
        fill = execute_leg(bid, ask, ltp, leg.quantity, is_buy, self.cfg)
        if fill is None:
            return OrderResult(
                filled=False,
                reason="no two-sided quote — a fill here would be fictional",
            )
        return OrderResult(
            filled=True, price=fill.price, charges=fill.charges,
            reason=f"paper fill at {fill.price:.2f} "
                   f"(mid {fill.reference:.2f}, slip {fill.slippage:.2f})",
            fill=fill,
        )


class LiveBroker(Broker):
    """
    Placeholder for real order placement.

    Intentionally not implemented. The abstraction is here so that live
    execution can be added without touching the trade manager, but turning it on
    means sending real orders with real money, and that belongs in a change made
    deliberately rather than arriving as a side effect.
    """

    def __init__(self, token: str):
        self.token = token

    @property
    def is_live(self) -> bool:
        return True

    def place(self, leg: TradeLeg, bid: float, ask: float, ltp: float,
              is_buy: bool) -> OrderResult:
        raise NotImplementedError(
            "Live execution is not implemented. Paper trading is the supported "
            "path; wiring real orders is a deliberate, separately reviewed step."
        )


def get_broker(live: bool = False, token: Optional[str] = None,
               cost_config: Optional[CostConfig] = None) -> Broker:
    """
    Broker factory. Paper unless `live` is explicitly True.

    The default is the safe one on purpose: a missing or mistyped config value
    should never be the thing that starts sending orders.
    """
    if live:
        if not token:
            raise ValueError("Live broker requires a broker token.")
        return LiveBroker(token)
    return PaperBroker(cost_config)
