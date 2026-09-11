# optionslens/backend/routers/trades.py
"""
Trade lifecycle and journal API.

GET    /api/trades                  → list trades, optionally by state
GET    /api/trades/journal          → closed-trade statistics
GET    /api/trades/portfolio        → live MTM, net Greeks, risk warnings
GET    /api/trades/{trade_id}       → one trade
POST   /api/trades/propose          → size a manual or signal-driven trade
POST   /api/trades/{trade_id}/enter → run entry gates and fill
POST   /api/trades/{trade_id}/close → flatten
POST   /api/trades/{trade_id}/journal → attach a postmortem

Everything is paper by default. There is no endpoint that places a live order.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_token
from chain_pricing import implied_forward_for_chain
from config import UNDERLYINGS
from fyers_client import fetch_option_chain, fetch_quote, get_fyers
from market_hours import time_to_expiry
from notify import get_dispatcher
from notify.events import trade_closed, trade_opened, trade_rejected
from trading.manager import TradeManager
from trading.models import ExitReason, TradeLeg, TradeState
from trading.portfolio import mark_trade, portfolio_risk
from trading.store import get_trade, journal_stats, list_trades, open_trades

router = APIRouter(prefix="/api/trades", tags=["trades"])
logger = logging.getLogger(__name__)


class LegRequest(BaseModel):
    strike:      float
    option_type: str          # CE / PE
    action:      str          # BUY / SELL
    lots:        int = 1


class ProposeRequest(BaseModel):
    symbol:       str
    expiry_date:  str
    expiry_epoch: int
    legs:         list[LegRequest]
    signal_id:      Optional[str] = None
    signal_version: Optional[int] = None
    direction:      Optional[str] = None
    strength:       Optional[float] = None


class JournalRequest(BaseModel):
    postmortem: str


def _chain_and_spot(token: str, symbol: str, expiry_epoch: int):
    fyers = get_fyers(token)
    spot  = fetch_quote(fyers, symbol)
    chain = fetch_option_chain(fyers, symbol, expiry_epoch, strike_count=20)
    return chain, spot


@router.get("")
def get_trades(state: Optional[str] = None, symbol: Optional[str] = None,
               limit: int = 100, token: str = Depends(get_token)):
    """List trades, newest first."""
    try:
        st = TradeState(state.upper()) if state else None
    except ValueError:
        raise HTTPException(400, f"Unknown state: {state}")

    trades = list_trades(st, symbol, limit)
    return {
        "count": len(trades),
        "trades": [
            {
                "trade_id": t.trade_id, "state": t.state.value, "symbol": t.symbol,
                "created_at": t.created_at, "opened_at": t.opened_at,
                "closed_at": t.closed_at,
                "exit_reason": t.exit_reason.value if t.exit_reason else None,
                "signal_id": t.signal_id, "direction": t.direction,
                "realized_pnl": t.realized_pnl, "risk_amount": t.risk_amount,
                "mae": t.mae, "mfe": t.mfe, "paper": t.paper,
                "notes": t.notes, "postmortem": t.postmortem,
                "legs": [
                    {"strike": l.strike, "option_type": l.option_type,
                     "action": l.action, "lots": l.lots,
                     "entry_price": l.entry_price, "exit_price": l.exit_price}
                    for l in t.legs
                ],
            }
            for t in trades
        ],
    }


@router.get("/journal")
def get_journal(token: str = Depends(get_token)):
    """Closed-trade statistics, including average MAE."""
    return journal_stats()


@router.get("/portfolio")
def get_portfolio(symbol: str = "NIFTY", expiry_epoch: Optional[int] = None,
                  expiry_date: Optional[str] = None,
                  token: str = Depends(get_token)):
    """Live MTM and aggregate risk across open positions."""
    trades = open_trades(symbol)
    if not trades:
        return {"open_positions": 0, "marks": [], "warnings": [],
                "note": "No open positions."}

    if expiry_epoch is None:
        expiry_date = expiry_date or trades[0].legs[0].expiry_date
        raise HTTPException(
            400, "expiry_epoch is required to fetch the chain for marking. "
                 f"Use /api/expiries/{symbol} to look it up for {expiry_date}.")

    chain, spot = _chain_and_spot(token, symbol, expiry_epoch)
    exp = expiry_date or trades[0].legs[0].expiry_date
    T = time_to_expiry(exp)
    forward = implied_forward_for_chain(chain, T, spot)

    marks = [mark_trade(t, chain, spot, forward, T) for t in trades]
    risk = portfolio_risk(trades, marks)

    return {
        "open_positions":   risk.open_positions,
        "total_unrealized": risk.total_unrealized,
        "committed_risk":   risk.committed_risk,
        "net_delta": risk.net_delta, "net_gamma": risk.net_gamma,
        "net_vega":  risk.net_vega,  "net_theta": risk.net_theta,
        "by_symbol": risk.by_symbol,
        "warnings":  risk.warnings,
        "marks": [vars(m) for m in risk.marks],
    }


@router.get("/{trade_id}")
def get_one(trade_id: str, token: str = Depends(get_token)):
    trade = get_trade(trade_id)
    if not trade:
        raise HTTPException(404, f"No such trade: {trade_id}")
    return {
        "trade_id": trade.trade_id, "state": trade.state.value,
        "symbol": trade.symbol, "describe": trade.describe(),
        "signal_id": trade.signal_id, "direction": trade.direction,
        "risk_amount": trade.risk_amount, "max_loss": trade.max_loss,
        "realized_pnl": trade.realized_pnl, "total_charges": trade.total_charges,
        "mae": trade.mae, "mfe": trade.mfe,
        "exit_reason": trade.exit_reason.value if trade.exit_reason else None,
        "notes": trade.notes, "postmortem": trade.postmortem,
        "paper": trade.paper, "meta": trade.meta,
        "legs": [vars(l) for l in trade.legs],
    }


@router.post("/propose")
async def propose(req: ProposeRequest, token: str = Depends(get_token)):
    """
    Size a trade and run entry gates. Paper fills only.

    A proposal that fails sizing or entry checks is still persisted as REJECTED
    with the reason — skipped trades are evidence too.
    """
    symbol = req.symbol.upper()
    if symbol not in UNDERLYINGS:
        raise HTTPException(400, f"Unknown symbol: {symbol}")
    if not req.legs:
        raise HTTPException(400, "At least one leg is required.")

    chain, spot = _chain_and_spot(token, symbol, req.expiry_epoch)
    if not chain:
        raise HTTPException(404, "No chain data returned.")

    lot_size = UNDERLYINGS[symbol]["lot_size"]
    by_key = {(r["strike"], r["option_type"]): r for r in chain}

    primary_key = (req.legs[0].strike, req.legs[0].option_type)
    primary_row = by_key.get(primary_key)
    if not primary_row:
        raise HTTPException(404, f"Strike {primary_key} not in chain.")

    legs = [
        TradeLeg(
            symbol=symbol, expiry_date=req.expiry_date, strike=l.strike,
            option_type=l.option_type, action=l.action.upper(),
            lots=l.lots, lot_size=lot_size,
        )
        for l in req.legs
    ]

    mgr = TradeManager()
    trade = mgr.propose(
        symbol=symbol, legs=legs, chain_row=primary_row, spot=spot,
        signal_id=req.signal_id, signal_version=req.signal_version,
        direction=req.direction, strength=req.strength,
    )

    if trade.state is TradeState.REJECTED:
        await get_dispatcher().send(trade_rejected(trade))
        return {"trade_id": trade.trade_id, "state": trade.state.value,
                "reason": trade.notes}

    return {"trade_id": trade.trade_id, "state": trade.state.value,
            "lots": trade.legs[0].lots, "risk_amount": trade.risk_amount,
            "max_loss": trade.max_loss, "sizing": trade.meta.get("sizing")}


@router.post("/{trade_id}/enter")
async def enter(trade_id: str, expiry_epoch: int, token: str = Depends(get_token)):
    """Run entry gates and paper-fill the proposal."""
    trade = get_trade(trade_id)
    if not trade:
        raise HTTPException(404, f"No such trade: {trade_id}")
    if trade.state is not TradeState.PROPOSED:
        raise HTTPException(409, f"Trade is {trade.state.value}, not PROPOSED.")

    chain, spot = _chain_and_spot(token, trade.symbol, expiry_epoch)
    by_key = {(r["strike"], r["option_type"]): r for r in chain}
    row = by_key.get((trade.legs[0].strike, trade.legs[0].option_type))
    if not row:
        raise HTTPException(404, "Primary leg not in chain.")

    mgr = TradeManager()
    trade = mgr.try_enter(trade, row, spot)

    dispatcher = get_dispatcher()
    if trade.state is TradeState.OPEN:
        await dispatcher.send(trade_opened(trade))
    else:
        await dispatcher.send(trade_rejected(trade))

    return {"trade_id": trade.trade_id, "state": trade.state.value,
            "notes": trade.notes,
            "entry_prices": [l.entry_price for l in trade.legs]}


@router.post("/{trade_id}/close")
async def close(trade_id: str, expiry_epoch: int, reason: str = "MANUAL",
                token: str = Depends(get_token)):
    """Flatten an open position."""
    trade = get_trade(trade_id)
    if not trade:
        raise HTTPException(404, f"No such trade: {trade_id}")
    if trade.state is not TradeState.OPEN:
        raise HTTPException(409, f"Trade is {trade.state.value}, not OPEN.")

    try:
        exit_reason = ExitReason(reason.upper())
    except ValueError:
        raise HTTPException(400, f"Unknown exit reason: {reason}")

    chain, spot = _chain_and_spot(token, trade.symbol, expiry_epoch)
    mgr = TradeManager()
    trade = mgr.close(trade, chain, spot, exit_reason)

    await get_dispatcher().send(trade_closed(trade))

    return {"trade_id": trade.trade_id, "state": trade.state.value,
            "realized_pnl": trade.realized_pnl,
            "total_charges": trade.total_charges,
            "exit_reason": trade.exit_reason.value,
            "suggested_postmortem": mgr.auto_postmortem(trade)}


@router.post("/{trade_id}/journal")
def journal(trade_id: str, req: JournalRequest, token: str = Depends(get_token)):
    """Attach a postmortem and mark the trade reviewed."""
    mgr = TradeManager()
    try:
        trade = mgr.journal(trade_id, req.postmortem)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(409, str(e))
    return {"trade_id": trade.trade_id, "state": trade.state.value,
            "postmortem": trade.postmortem}
