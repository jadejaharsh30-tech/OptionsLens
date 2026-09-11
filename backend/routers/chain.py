# optionslens/backend/routers/chain.py
"""
GET /api/chain/{symbol}?expiry_epoch=<int>&expiry_date=<DD-MM-YYYY>
Fetches live option chain for one expiry, enriched with IV and Greeks.
IV is calculated via Newton-Raphson on Black-Scholes (not from Fyers).
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from auth import get_token
from fyers_client import fetch_quote, fetch_option_chain, get_fyers
from iv_engine import implied_volatility, greeks
from config import UNDERLYINGS, RISK_FREE_RATE

# Time-to-expiry lives in market_hours (the session-timing single source of
# truth). Re-exported here because several modules still import it from this
# router; prefer importing from market_hours directly in new code.
from market_hours import days_to_expiry, time_to_expiry  # noqa: F401

router = APIRouter(prefix="/api/chain", tags=["chain"])


@router.get("/{symbol}")
def get_chain(
    symbol:       str,
    expiry_epoch: int = Query(..., description="Expiry epoch from /api/expiries"),
    expiry_date:  str = Query(..., description="Expiry date string e.g. '24-04-2025'"),
    token:        str = Depends(get_token),
):
    """
    Returns IV-enriched option chain for one expiry.
    Each row includes: strike, option_type, oi, oi_change, ltp, volume,
                       iv (%), delta, gamma, vega, theta, rho, is_atm
    """
    symbol = symbol.upper()
    if symbol not in UNDERLYINGS:
        raise HTTPException(400, f"Unknown symbol: {symbol}")

    fyers = get_fyers(token)
    spot  = fetch_quote(fyers, symbol)
    T     = days_to_expiry(expiry_date)
    chain = fetch_option_chain(fyers, symbol, expiry_epoch, strike_count=20)

    if not chain:
        raise HTTPException(404, f"No chain data returned for {symbol} / {expiry_date}")

    # Find ATM strike once (min abs distance from spot)
    atm_strike = min(
        set(opt["strike"] for opt in chain),
        key=lambda k: abs(k - spot)
    )

    enriched = []
    for opt in chain:
        iv = None
        g  = {}

        if opt["ltp"] > 0 and T > 0:
            iv = implied_volatility(
                market_price=opt["ltp"],
                S=spot,
                K=opt["strike"],
                T=T,
                r=RISK_FREE_RATE,
                option_type=opt["option_type"],
            )
            if iv is not None:
                g = greeks(spot, opt["strike"], T, RISK_FREE_RATE, iv, opt["option_type"])

        enriched.append({
            **opt,
            "iv":     round(iv * 100, 2) if iv is not None else None,  # stored as % e.g. 14.5
            "delta":  g.get("delta"),
            "gamma":  g.get("gamma"),
            "vega":   g.get("vega"),
            "theta":  g.get("theta"),
            "rho":    g.get("rho"),
            "is_atm": opt["strike"] == atm_strike,
        })

    enriched.sort(key=lambda x: x["strike"])

    return {
        "symbol":       symbol,
        "spot":         spot,
        "expiry_date":  expiry_date,
        "atm_strike":   atm_strike,
        "T":            round(T, 6),
        "chain":        enriched,
    }
