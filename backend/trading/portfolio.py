# optionslens/backend/trading/portfolio.py
"""
Portfolio-level mark-to-market and risk.

Per-trade risk is not portfolio risk. Five separate "small" long-call positions
on NIFTY are one large long-delta, long-vega bet, and each one individually
passing its risk check tells you nothing about that. This module aggregates so
the concentration is visible before it is discovered by a gap.
"""
from dataclasses import dataclass, field
from typing import Optional

from chain_pricing import price_for_iv
from config import RISK_FREE_RATE, UNDERLYINGS
from iv_engine import black76_greeks, implied_vol_forward
from trading.models import Trade


@dataclass
class PositionMark:
    """One trade marked to current prices."""
    trade_id:       str
    symbol:         str
    unrealized_pnl: Optional[float]
    pnl_pct:        Optional[float]
    delta:          Optional[float] = None
    gamma:          Optional[float] = None
    vega:           Optional[float] = None
    theta:          Optional[float] = None
    iv:             Optional[float] = None
    markable:       bool = True
    note:           str = ""


@dataclass
class PortfolioRisk:
    """Aggregate exposure across every open position."""
    open_positions: int = 0
    total_unrealized: float = 0.0
    committed_risk:   float = 0.0
    net_delta:  float = 0.0
    net_gamma:  float = 0.0
    net_vega:   float = 0.0
    net_theta:  float = 0.0
    by_symbol:  dict[str, dict] = field(default_factory=dict)
    warnings:   list[str] = field(default_factory=list)
    marks:      list[PositionMark] = field(default_factory=list)


def build_marks(chain: list[dict]) -> dict[tuple[float, str], float]:
    """(strike, option_type) -> mark price, from a live or recorded chain."""
    out: dict[tuple[float, str], float] = {}
    for row in chain:
        price = price_for_iv(row)
        if price:
            out[(row["strike"], row["option_type"])] = price
    return out


def mark_trade(trade: Trade, chain: list[dict], spot: float, forward: Optional[float],
               T: float) -> PositionMark:
    """
    Mark one trade and compute its position Greeks.

    Greeks are recomputed from current prices rather than carried from entry —
    an option's delta at entry tells you nothing about its delta now, and stale
    Greeks are how a position drifts into a different trade unnoticed.
    """
    marks = build_marks(chain)
    unrealized = trade.unrealized_pnl(marks)

    if unrealized is None:
        return PositionMark(
            trade_id=trade.trade_id, symbol=trade.symbol,
            unrealized_pnl=None, pnl_pct=None, markable=False,
            note="one or more legs have no usable quote",
        )

    entry_cost = abs(trade.entry_cost())
    pnl_pct = (unrealized / entry_cost * 100.0) if entry_cost > 0 else None

    net = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    ivs: list[float] = []

    if forward and T > 0:
        by_key = {(r["strike"], r["option_type"]): r for r in chain}
        for leg in trade.legs:
            row = by_key.get((leg.strike, leg.option_type))
            if not row:
                continue
            price = price_for_iv(row)
            if not price:
                continue
            iv = implied_vol_forward(price, forward, leg.strike, T,
                                     RISK_FREE_RATE, leg.option_type)
            if iv is None:
                continue
            ivs.append(iv)
            g = black76_greeks(forward, leg.strike, T, RISK_FREE_RATE, iv,
                               leg.option_type, spot=spot)
            for k in net:
                net[k] += g[k] * leg.sign * leg.lots

    return PositionMark(
        trade_id=trade.trade_id, symbol=trade.symbol,
        unrealized_pnl=round(unrealized, 2),
        pnl_pct=round(pnl_pct, 2) if pnl_pct is not None else None,
        delta=round(net["delta"], 4), gamma=round(net["gamma"], 6),
        vega=round(net["vega"], 4), theta=round(net["theta"], 4),
        iv=round(sum(ivs) / len(ivs), 4) if ivs else None,
    )


def portfolio_risk(trades: list[Trade], marks: list[PositionMark],
                   capital: float = 500_000.0,
                   max_symbol_concentration_pct: float = 60.0) -> PortfolioRisk:
    """
    Aggregate across positions and flag the concentrations that matter.

    Warnings are deliberately about direction of exposure rather than P&L: net
    short vega across the book is a statement about what a vol spike does to
    you, and it is worth seeing before the spike rather than during it.
    """
    risk = PortfolioRisk(open_positions=len(trades))

    for t, m in zip(trades, marks):
        risk.committed_risk += t.risk_amount or 0.0
        if m.unrealized_pnl is not None:
            risk.total_unrealized += m.unrealized_pnl
        for attr in ("delta", "gamma", "vega", "theta"):
            val = getattr(m, attr) or 0.0
            setattr(risk, f"net_{attr}", getattr(risk, f"net_{attr}") + val)

        bucket = risk.by_symbol.setdefault(
            t.symbol, {"positions": 0, "risk": 0.0, "unrealized": 0.0, "delta": 0.0},
        )
        bucket["positions"] += 1
        bucket["risk"] += t.risk_amount or 0.0
        bucket["unrealized"] += m.unrealized_pnl or 0.0
        bucket["delta"] += m.delta or 0.0

    for k in ("net_delta", "net_gamma", "net_vega", "net_theta"):
        setattr(risk, k, round(getattr(risk, k), 4))
    risk.committed_risk = round(risk.committed_risk, 2)
    risk.total_unrealized = round(risk.total_unrealized, 2)
    risk.marks = marks

    # ── Warnings ─────────────────────────────────────────────────────────────
    if risk.committed_risk > capital * 0.10:
        risk.warnings.append(
            f"Committed risk {risk.committed_risk:.0f} exceeds 10% of capital."
        )

    for sym, b in risk.by_symbol.items():
        if risk.committed_risk > 0:
            share = b["risk"] / risk.committed_risk * 100.0
            if share > max_symbol_concentration_pct and len(risk.by_symbol) > 1:
                risk.warnings.append(
                    f"{share:.0f}% of risk sits in {sym} — these positions will "
                    f"move together, not independently."
                )

    if risk.net_vega < 0:
        risk.warnings.append(
            f"Net short vega ({risk.net_vega:.2f}) — a vol spike hurts the whole "
            f"book at once."
        )
    if risk.net_gamma < 0:
        risk.warnings.append(
            f"Net short gamma ({risk.net_gamma:.6f}) — losses accelerate as the "
            f"underlying moves."
        )

    unmarked = sum(1 for m in marks if not m.markable)
    if unmarked:
        risk.warnings.append(
            f"{unmarked} position(s) could not be marked — P&L shown is incomplete."
        )

    return risk


def lot_size_for(symbol: str) -> int:
    cfg = UNDERLYINGS.get(symbol.upper())
    return cfg["lot_size"] if cfg else 1
