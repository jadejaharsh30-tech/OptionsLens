# optionslens/backend/routers/chain.py
"""
GET /api/chain/{symbol}?expiry_epoch=<int>&expiry_date=<DD-MM-YYYY>
Fetches live option chain for one expiry, enriched with IV and Greeks.

IV is solved by us, not supplied by Fyers. Pricing is Black-76 off the forward
implied by the chain's own put-call parity, so calls and puts at one strike
share a single forward and their IVs agree. Pricing off spot instead inflates
call IVs and deflates put IVs by roughly the dividend yield, which shows up as
fake skew on the surface chart.
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from auth import get_token
from chain_pricing import implied_forward_for_chain, price_for_iv
from forward_engine import forward_basis_pct, implied_dividend_yield
from fyers_client import fetch_quote, fetch_option_chain, get_fyers
from iv_engine import black76_greeks, implied_vol_forward
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

    forward = implied_forward_for_chain(chain, T, spot)

    enriched = []
    for opt in chain:
        iv = None
        g  = {}

        price = price_for_iv(opt)
        if price and T > 0 and forward:
            iv = implied_vol_forward(
                market_price=price,
                F=forward,
                K=opt["strike"],
                T=T,
                r=RISK_FREE_RATE,
                option_type=opt["option_type"],
            )
            if iv is not None:
                g = black76_greeks(forward, opt["strike"], T, RISK_FREE_RATE,
                                   iv, opt["option_type"], spot=spot)

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
        "forward":      round(forward, 2) if forward else None,
        "basis_pct":    round(forward_basis_pct(forward, spot), 4) if forward else None,
        "implied_div_yield": (
            round(implied_dividend_yield(forward, spot, T, RISK_FREE_RATE) * 100, 3)
            if forward and T > 0 else None
        ),
        "expiry_date":  expiry_date,
        "atm_strike":   atm_strike,
        "T":            round(T, 6),
        "chain":        enriched,
    }
