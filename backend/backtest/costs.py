# optionslens/backend/backtest/costs.py
"""
Transaction cost model for Indian index options.

Costs are where most paper edges die. An options signal showing 0.4% average
edge is worthless if a round trip costs 2% — and for options the dominant cost
is almost never the taxes, it is the **bid-ask spread**. A model that charges
brokerage and STT but fills at mid is not conservative, it is wrong: you cannot
trade at mid, and on an illiquid strike the spread alone can exceed the entire
signal.

So the default fill is spread-aware: buy at the ask, sell at the bid, plus
slippage. Mid-fills are available for sensitivity analysis and are labelled as
optimistic, not realistic.

RATE DISCLAIMER: the statutory rates below were correct to the best of our
knowledge when written and DO change (STT on options premium was revised in
Oct 2024, for instance). They are parameters, not constants — verify against
current exchange and CBDT circulars before trusting a live P&L figure.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class FillModel(str, Enum):
    """How aggressively we assume we get filled."""
    SPREAD    = "SPREAD"     # buy at ask, sell at bid — the realistic default
    MID       = "MID"        # optimistic; sensitivity analysis only
    HALF      = "HALF"       # meet halfway; plausible with patient limit orders


@dataclass(frozen=True)
class CostConfig:
    """
    All rates in decimal (0.001 = 0.1%). Defaults approximate a discount broker
    on NSE index options.
    """
    brokerage_per_order:   float = 20.0     # flat INR per executed order
    stt_sell_premium:      float = 0.001    # STT on options SELL, on premium
    exchange_txn_pct:      float = 0.00035  # NSE transaction charge on premium
    sebi_charges_pct:      float = 0.000001 # ~INR 10 per crore
    stamp_duty_buy_pct:    float = 0.00003  # buy side only
    gst_pct:               float = 0.18     # on brokerage + txn + SEBI charges
    slippage_ticks:        float = 1.0      # additional ticks beyond the quote
    tick_size:             float = 0.05
    fill_model:            FillModel = FillModel.SPREAD


@dataclass
class Fill:
    """One executed leg."""
    price:       float       # price actually paid/received per unit
    quantity:    int         # contracts (lots * lot_size)
    is_buy:      bool
    charges:     float       # all statutory + brokerage costs, INR
    slippage:    float       # cost of crossing the spread + slippage, per unit
    reference:   float       # mid at decision time, for attribution


def fill_price(bid: float, ask: float, ltp: float, is_buy: bool,
               cfg: CostConfig) -> Optional[float]:
    """
    The price we assume we transact at.

    Returns None when the book is unusable — a one-sided or empty book is not
    a tradeable market, and inventing a fill there is how backtests manufacture
    profits that cannot be realised.
    """
    has_book = bid > 0 and ask > 0
    if not has_book:
        return None

    mid  = (bid + ask) / 2.0
    slip = cfg.slippage_ticks * cfg.tick_size

    if cfg.fill_model is FillModel.MID:
        base = mid
    elif cfg.fill_model is FillModel.HALF:
        base = (mid + ask) / 2.0 if is_buy else (mid + bid) / 2.0
    else:                                    # SPREAD
        base = ask if is_buy else bid

    price = base + slip if is_buy else base - slip
    return max(price, cfg.tick_size)


def leg_charges(price: float, quantity: int, is_buy: bool,
                cfg: CostConfig) -> float:
    """Statutory charges plus brokerage for one leg, in INR."""
    turnover = price * quantity

    brokerage = cfg.brokerage_per_order
    stt       = 0.0 if is_buy else turnover * cfg.stt_sell_premium
    exchange  = turnover * cfg.exchange_txn_pct
    sebi      = turnover * cfg.sebi_charges_pct
    stamp     = turnover * cfg.stamp_duty_buy_pct if is_buy else 0.0
    gst       = (brokerage + exchange + sebi) * cfg.gst_pct

    return brokerage + stt + exchange + sebi + stamp + gst


def execute_leg(bid: float, ask: float, ltp: float, quantity: int,
                is_buy: bool, cfg: CostConfig) -> Optional[Fill]:
    """Simulate one leg. None when the book cannot support a fill."""
    price = fill_price(bid, ask, ltp, is_buy, cfg)
    if price is None:
        return None

    mid = (bid + ask) / 2.0
    return Fill(
        price     = price,
        quantity  = quantity,
        is_buy    = is_buy,
        charges   = leg_charges(price, quantity, is_buy, cfg),
        slippage  = abs(price - mid),
        reference = mid,
    )


def round_trip_cost(entry: Fill, exit_: Fill) -> dict:
    """
    Total cost of a completed round trip, split by source.

    The split matters: if spread dominates, the fix is better strike selection
    or patient limit orders. If charges dominate, the fix is trading less. They
    are different problems and the aggregate number hides which one you have.
    """
    spread_cost = (entry.slippage + exit_.slippage) * entry.quantity
    charges     = entry.charges + exit_.charges
    return {
        "spread_and_slippage": round(spread_cost, 2),
        "statutory_and_brokerage": round(charges, 2),
        "total": round(spread_cost + charges, 2),
    }


def net_pnl(entry: Fill, exit_: Fill) -> float:
    """
    Realised P&L after all costs, in INR.

    Spread cost is already embedded in the fill prices, so it must not be
    subtracted again here — only the statutory charges are.
    """
    direction = 1 if entry.is_buy else -1
    gross = direction * (exit_.price - entry.price) * entry.quantity
    return gross - entry.charges - exit_.charges
