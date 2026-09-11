# optionslens/backend/trading/models.py
"""
Trade lifecycle data model.

    SIGNAL -> PROPOSED -> OPEN -> CLOSED -> JOURNALED
                  |         |
                  v         v
              REJECTED   (exit rules)

A trade is multi-leg from the start. Single-leg is just the degenerate case, and
retrofitting spreads onto a single-leg model later is how position tracking
quietly becomes wrong.

Every transition is validated. An invalid transition raises rather than silently
correcting, because a position that reaches an impossible state is a position
whose P&L you cannot trust.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class TradeState(str, Enum):
    SIGNAL    = "SIGNAL"      # a signal fired; nothing sized yet
    PROPOSED  = "PROPOSED"    # sized, entry rules checked, awaiting fill
    OPEN      = "OPEN"        # filled and live
    CLOSED    = "CLOSED"      # flat, P&L realised
    JOURNALED = "JOURNALED"   # reviewed, postmortem written
    REJECTED  = "REJECTED"    # never entered (spread too wide, no budget, stale)


# The only transitions that may occur. Anything else is a bug.
LEGAL_TRANSITIONS: dict[TradeState, set[TradeState]] = {
    TradeState.SIGNAL:    {TradeState.PROPOSED, TradeState.REJECTED},
    TradeState.PROPOSED:  {TradeState.OPEN, TradeState.REJECTED},
    TradeState.OPEN:      {TradeState.CLOSED},
    TradeState.CLOSED:    {TradeState.JOURNALED},
    TradeState.JOURNALED: set(),
    TradeState.REJECTED:  set(),
}


class ExitReason(str, Enum):
    STOP_LOSS      = "STOP_LOSS"
    TARGET         = "TARGET"
    TIME_STOP      = "TIME_STOP"        # held too long; theta bleed
    IV_CRUSH       = "IV_CRUSH"         # vol collapsed out from under a long
    DELTA_DRIFT    = "DELTA_DRIFT"      # position no longer the trade we put on
    EXPIRY_FLATTEN = "EXPIRY_FLATTEN"   # hard close before expiry
    SIGNAL_REVERSE = "SIGNAL_REVERSE"
    MANUAL         = "MANUAL"


class InvalidTransition(Exception):
    """Raised on an illegal state change."""


@dataclass
class TradeLeg:
    """One option contract in a position."""
    symbol:      str
    expiry_date: str
    strike:      float
    option_type: str          # CE / PE
    action:      str          # BUY / SELL
    lots:        int
    lot_size:    int

    entry_price: Optional[float] = None
    exit_price:  Optional[float] = None
    entry_iv:    Optional[float] = None
    exit_iv:     Optional[float] = None
    entry_delta: Optional[float] = None
    charges:     float = 0.0

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size

    @property
    def sign(self) -> int:
        return 1 if self.action == "BUY" else -1

    @property
    def is_buy(self) -> bool:
        return self.action == "BUY"

    def gross_pnl(self, mark: Optional[float] = None) -> Optional[float]:
        """Unrealised (with `mark`) or realised (without) P&L, before charges."""
        price = mark if mark is not None else self.exit_price
        if self.entry_price is None or price is None:
            return None
        return self.sign * (price - self.entry_price) * self.quantity

    def describe(self) -> str:
        return (f"{self.action} {self.lots}x {self.symbol} {self.strike:.0f}"
                f"{self.option_type} {self.expiry_date}")


@dataclass
class Trade:
    """A position through its whole life."""
    trade_id:     str
    state:        TradeState
    symbol:       str
    legs:         list[TradeLeg]
    created_at:   str

    # Provenance — which signal, which version, which params produced this.
    # Without it a journal entry cannot be tied back to a hypothesis.
    signal_id:      Optional[str] = None
    signal_version: Optional[int] = None
    signal_ts:      Optional[str] = None
    direction:      Optional[str] = None
    strength:       Optional[float] = None

    opened_at:   Optional[str] = None
    closed_at:   Optional[str] = None
    exit_reason: Optional[ExitReason] = None

    # Risk, fixed at entry
    risk_amount:   Optional[float] = None    # INR at risk when sized
    max_loss:      Optional[float] = None
    entry_spot:    Optional[float] = None
    exit_spot:     Optional[float] = None

    # Excursions, tracked while open
    mae:           Optional[float] = None    # worst unrealised P&L seen (INR)
    mfe:           Optional[float] = None    # best unrealised P&L seen (INR)
    mae_at:        Optional[str] = None
    mfe_at:        Optional[str] = None

    realized_pnl:  Optional[float] = None    # net of all charges
    total_charges: float = 0.0

    paper:         bool = True               # paper unless explicitly live
    notes:         Optional[str] = None
    postmortem:    Optional[str] = None
    meta:          dict[str, Any] = field(default_factory=dict)

    # ── State machine ────────────────────────────────────────────────────────

    def transition(self, to: TradeState):
        allowed = LEGAL_TRANSITIONS.get(self.state, set())
        if to not in allowed:
            raise InvalidTransition(
                f"{self.trade_id}: cannot go {self.state.value} -> {to.value}. "
                f"Legal: {sorted(s.value for s in allowed) or 'none (terminal)'}"
            )
        self.state = to

    @property
    def is_open(self) -> bool:
        return self.state is TradeState.OPEN

    @property
    def is_terminal(self) -> bool:
        return not LEGAL_TRANSITIONS.get(self.state, set())

    # ── P&L ──────────────────────────────────────────────────────────────────

    def entry_cost(self) -> float:
        """Net premium paid (positive) or received (negative), before charges."""
        total = 0.0
        for leg in self.legs:
            if leg.entry_price is None:
                continue
            total += leg.sign * leg.entry_price * leg.quantity
        return total

    def unrealized_pnl(self, marks: dict[tuple[float, str], float]) -> Optional[float]:
        """
        Mark-to-market against current prices, keyed (strike, option_type).

        Returns None if any leg is unmarkable — a partially marked multi-leg
        position is not a number worth showing, since the missing leg is often
        the one that moved.
        """
        total = 0.0
        for leg in self.legs:
            mark = marks.get((leg.strike, leg.option_type))
            pnl = leg.gross_pnl(mark)
            if pnl is None:
                return None
            total += pnl
        return total - self.total_charges

    def update_excursions(self, unrealized: float, ts: str):
        """Track MAE/MFE while open — the record of how much heat it took."""
        if self.mae is None or unrealized < self.mae:
            self.mae, self.mae_at = unrealized, ts
        if self.mfe is None or unrealized > self.mfe:
            self.mfe, self.mfe_at = unrealized, ts

    def describe(self) -> str:
        return f"{self.trade_id} [{self.state.value}] " + " | ".join(
            leg.describe() for leg in self.legs
        )
