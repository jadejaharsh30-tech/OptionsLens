# optionslens/backend/routers/position_lab.py
"""
POST /api/position-lab/calculate
Pure math endpoint — no Fyers connection needed.
Frontend sends a list of legs, gets back:
  - Net Greeks for the full position
  - Per-leg Greeks breakdown
  - Payoff diagram data (P&L at expiry + P&L today)
    across a configurable spot price range
"""
from fastapi import APIRouter
from pydantic import BaseModel
from iv_engine import bs_price, greeks
from config import RISK_FREE_RATE

router = APIRouter(prefix="/api/position-lab", tags=["position-lab"])

DIRECTION = {"BUY": 1, "SELL": -1}


class Leg(BaseModel):
    strike:      float
    T:           float   # time to expiry in years (e.g. 30/365 = 0.082)
    option_type: str     # "CE" or "PE"
    action:      str     # "BUY" or "SELL"
    qty:         int     # number of lots
    iv:          float   # implied vol as decimal (e.g. 0.14 for 14%)


class PositionRequest(BaseModel):
    spot:            float
    legs:            list[Leg]
    spot_range_pct:  float = 15.0   # payoff x-axis: spot ± this %
    num_points:      int   = 101    # number of points in payoff curve


@router.post("/calculate")
def calculate_position(req: PositionRequest):
    """
    Calculates net Greeks and payoff diagram for a multi-leg strategy.
    No auth required — pure computation.

    Example strategies:
      Long call:       1 leg, BUY CE
      Long straddle:   2 legs, BUY CE + BUY PE at same strike
      Bull call spread: 2 legs, BUY lower CE + SELL higher CE
      Iron condor:     4 legs
    """
    # ── Net Greeks + per-leg breakdown ──
    net_greeks = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0}
    leg_details = []

    for leg in req.legs:
        sign = DIRECTION[leg.action]
        g    = greeks(req.spot, leg.strike, leg.T, RISK_FREE_RATE, leg.iv, leg.option_type)
        ltp  = bs_price(req.spot, leg.strike, leg.T, RISK_FREE_RATE, leg.iv, leg.option_type)

        for key in net_greeks:
            net_greeks[key] += sign * leg.qty * g[key]

        leg_details.append({
            "strike":      leg.strike,
            "T":           leg.T,
            "option_type": leg.option_type,
            "action":      leg.action,
            "qty":         leg.qty,
            "iv_pct":      round(leg.iv * 100, 2),
            "ltp":         round(ltp, 2),
            "delta":       round(sign * leg.qty * g["delta"], 4),
            "gamma":       round(sign * leg.qty * g["gamma"], 6),
            "vega":        round(sign * leg.qty * g["vega"],  4),
            "theta":       round(sign * leg.qty * g["theta"], 4),
            "rho":         round(sign * leg.qty * g["rho"],   4),
        })

    # ── Payoff diagram ──
    spot_lo    = req.spot * (1 - req.spot_range_pct / 100)
    spot_hi    = req.spot * (1 + req.spot_range_pct / 100)
    step       = (spot_hi - spot_lo) / (req.num_points - 1)
    spot_range = [spot_lo + i * step for i in range(req.num_points)]

    # Entry cost per leg (used to compute P&L relative to entry)
    entry_prices = [
        bs_price(req.spot, leg.strike, leg.T, RISK_FREE_RATE, leg.iv, leg.option_type)
        for leg in req.legs
    ]

    pnl_expiry = []
    pnl_today  = []

    for s in spot_range:
        pnl_exp = 0.0
        pnl_tod = 0.0
        for leg, entry in zip(req.legs, entry_prices):
            sign = DIRECTION[leg.action]
            # At expiry: intrinsic value only (T → 0)
            exp_price = bs_price(s, leg.strike, 1e-9, RISK_FREE_RATE, leg.iv, leg.option_type)
            # Today: full BS price at new spot
            tod_price = bs_price(s, leg.strike, leg.T,  RISK_FREE_RATE, leg.iv, leg.option_type)
            pnl_exp += sign * leg.qty * (exp_price - entry)
            pnl_tod += sign * leg.qty * (tod_price - entry)

        pnl_expiry.append(round(pnl_exp, 2))
        pnl_today.append(round(pnl_tod,  2))

    # ── Breakeven points (sign changes in expiry P&L) ──
    breakevens = []
    for i in range(len(pnl_expiry) - 1):
        if pnl_expiry[i] * pnl_expiry[i + 1] < 0:
            # Linear interpolation
            s0, s1 = spot_range[i], spot_range[i + 1]
            p0, p1 = pnl_expiry[i], pnl_expiry[i + 1]
            be = s0 - p0 * (s1 - s0) / (p1 - p0)
            breakevens.append(round(be, 2))

    return {
        "net_greeks":  {k: round(v, 4) for k, v in net_greeks.items()},
        "leg_details": leg_details,
        "payoff": {
            "spot_range": [round(s, 2) for s in spot_range],
            "pnl_expiry": pnl_expiry,
            "pnl_today":  pnl_today,
            "breakevens": breakevens,
        },
    }
