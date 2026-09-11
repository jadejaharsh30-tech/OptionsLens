# optionslens/backend/routers/oi.py
"""
GET /api/oi/{symbol}?expiry_epoch=<int>&expiry_date=<DD-MM-YYYY>
OI analysis for one expiry:
  - OI table by strike (calls vs puts, change vs prev session)
  - GEX profile per strike
  - Max Pain strike
  - PCR (Put-Call Ratio)

Uses callOi / putOi from Fyers top-level response as aggregate totals.
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from auth import get_token
from chain_pricing import implied_forward_for_chain, price_for_iv
from fyers_client import fetch_option_chain, fetch_quote, get_fyers
from iv_engine import black76_greeks, implied_vol_forward
from gex_engine import compute_net_gex_profile
from config import UNDERLYINGS, RISK_FREE_RATE
from market_hours import time_to_expiry as days_to_expiry

router = APIRouter(prefix="/api/oi", tags=["oi"])


def compute_max_pain(chain: list[dict]) -> float:
    """
    Max Pain = strike where sum of (ITM OI × intrinsic value) is minimised.
    This is the price at which option writers lose the least at expiry.
    """
    strikes = sorted(set(o["strike"] for o in chain))
    pain: dict[float, float] = {}

    for test_strike in strikes:
        total = 0.0
        for opt in chain:
            if opt["option_type"] == "CE":
                # Call writers lose when spot > strike
                total += max(test_strike - opt["strike"], 0) * opt["oi"]
            else:
                # Put writers lose when spot < strike
                total += max(opt["strike"] - test_strike, 0) * opt["oi"]
        pain[test_strike] = total

    return min(pain, key=pain.get)


@router.get("/{symbol}")
def get_oi_analysis(
    symbol:       str,
    expiry_epoch: int = Query(...),
    expiry_date:  str = Query(...),
    token:        str = Depends(get_token),
):
    """
    Returns full OI analysis for one expiry.

    Response shape:
    {
      symbol, spot, expiry_date,
      pcr,             # Put-Call Ratio (total OI basis)
      max_pain,        # Strike where option writers lose least
      total_call_oi,
      total_put_oi,
      oi_table: [{strike, call_oi, put_oi, call_oi_change,
                  put_oi_change, call_ltp, put_ltp}],
      gex_profile: [{strike, call_gex, put_gex, net_gex}]
    }
    """
    symbol = symbol.upper()
    if symbol not in UNDERLYINGS:
        raise HTTPException(400, f"Unknown symbol: {symbol}")

    cfg   = UNDERLYINGS[symbol]
    fyers = get_fyers(token)
    spot  = fetch_quote(fyers, symbol)
    T     = days_to_expiry(expiry_date)
    chain = fetch_option_chain(fyers, symbol, expiry_epoch, strike_count=20)

    if not chain:
        raise HTTPException(404, f"No chain data for {symbol} / {expiry_date}")

    # ── Aggregate OI totals ──
    total_call_oi = sum(o["oi"] for o in chain if o["option_type"] == "CE")
    total_put_oi  = sum(o["oi"] for o in chain if o["option_type"] == "PE")
    pcr = round(total_put_oi / total_call_oi, 3) if total_call_oi > 0 else None

    # ── Max Pain ──
    max_pain = compute_max_pain(chain)

    # One forward per expiry, shared by every strike's gamma calculation.
    forward = implied_forward_for_chain(chain, T, spot)

    # ── Per-strike OI table + GEX inputs ──
    by_strike: dict[float, dict] = {}
    gex_inputs: list[dict] = []

    for opt in chain:
        s = opt["strike"]
        if s not in by_strike:
            by_strike[s] = {
                "strike":          s,
                "call_oi":         0,
                "put_oi":          0,
                "call_oi_change":  0,
                "put_oi_change":   0,
                "call_ltp":        0.0,
                "put_ltp":         0.0,
            }

        if opt["option_type"] == "CE":
            by_strike[s]["call_oi"]        = opt["oi"]
            by_strike[s]["call_oi_change"] = opt["oi_change"]
            by_strike[s]["call_ltp"]       = opt["ltp"]
        else:
            by_strike[s]["put_oi"]         = opt["oi"]
            by_strike[s]["put_oi_change"]  = opt["oi_change"]
            by_strike[s]["put_ltp"]        = opt["ltp"]

        # Compute gamma for GEX
        # Use mid-price (bid+ask average) when available — more stable for IV solving
        # than LTP which can be stale. Fall back to LTP if bid/ask not available.
        # Gamma for GEX. Solved off the same implied forward the rest of the app
        # uses, so a strike's gamma here matches its gamma in /api/chain.
        # T is measured to the real 15:30 IST expiry instant; the old
        # `max(T, 1/365)` floor is gone — with two hours left it inflated T
        # ~12x and badly understated IV.
        gamma = 0.0
        price = price_for_iv(opt)

        if price and T > 0 and forward:
            iv = implied_vol_forward(
                market_price=price,
                F=forward, K=s, T=T,
                r=RISK_FREE_RATE,
                option_type=opt["option_type"],
            )
            if iv is not None:
                g = black76_greeks(forward, s, T, RISK_FREE_RATE,
                                   iv, opt["option_type"], spot=spot)
                gamma = g.get("gamma", 0.0)

        gex_inputs.append({
            "strike":      s,
            "option_type": opt["option_type"],
            "gamma":       gamma,
            "oi":          opt["oi"],
        })

    oi_table    = sorted(by_strike.values(), key=lambda x: x["strike"])
    gex_profile = compute_net_gex_profile(gex_inputs, cfg["lot_size"], spot)

    return {
        "symbol":        symbol,
        "spot":          spot,
        "forward":       round(forward, 2) if forward else None,
        "expiry_date":   expiry_date,
        "pcr":           pcr,
        "max_pain":      max_pain,
        "total_call_oi": total_call_oi,
        "total_put_oi":  total_put_oi,
        "oi_table":      oi_table,
        "gex_profile":   gex_profile,
    }
