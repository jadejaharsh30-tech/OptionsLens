# optionslens/backend/routers/surface.py
"""
GET /api/surface/{symbol}
Fetches IV surface across all available expiries.
Returns data for three frontend charts:
  1. 3D IV surface   (strike × days_to_expiry × IV)
  2. Skew curves     (per expiry: strike × call_iv / put_iv)
  3. Term structure  (ATM IV across expiries)
"""
from fastapi import APIRouter, Depends, HTTPException
from auth import get_token
from fyers_client import fetch_expiry_list, fetch_option_chain, fetch_quote, get_fyers
from iv_engine import implied_volatility
from config import UNDERLYINGS, RISK_FREE_RATE
from routers.chain import days_to_expiry

router = APIRouter(prefix="/api/surface", tags=["surface"])

# Max expiries to fetch — keeps response time reasonable
MAX_EXPIRIES = 6


@router.get("/{symbol}")
def get_iv_surface(symbol: str, token: str = Depends(get_token)):
    """
    Returns IV surface data across up to 6 expiries.

    Response shape:
    {
      symbol, spot,
      surface: [{expiry_date, days_to_expiry, strike, moneyness,
                 call_iv, put_iv, mid_iv}],
      term_structure: [{expiry_date, days_to_expiry, atm_iv}],
      skew: {
        "<expiry_date>": [{strike, moneyness, call_iv, put_iv}]
      }
    }
    """
    symbol = symbol.upper()
    if symbol not in UNDERLYINGS:
        raise HTTPException(400, f"Unknown symbol: {symbol}")

    fyers    = get_fyers(token)
    spot     = fetch_quote(fyers, symbol)
    expiries = fetch_expiry_list(fyers, symbol)

    surface_rows  = []
    term_structure = []
    skew: dict    = {}

    for exp in expiries[:MAX_EXPIRIES]:
        T = days_to_expiry(exp["date"])
        if T <= 0:
            continue

        chain = fetch_option_chain(fyers, symbol, exp["expiry"], strike_count=16)

        call_ivs: dict[float, float] = {}
        put_ivs:  dict[float, float] = {}

        for opt in chain:
            if opt["ltp"] <= 0:
                continue
            iv = implied_volatility(
                market_price=opt["ltp"],
                S=spot,
                K=opt["strike"],
                T=T,
                r=RISK_FREE_RATE,
                option_type=opt["option_type"],
            )
            if iv is None:
                continue
            iv_pct = round(iv * 100, 2)
            if opt["option_type"] == "CE":
                call_ivs[opt["strike"]] = iv_pct
            else:
                put_ivs[opt["strike"]] = iv_pct

        all_strikes = sorted(set(list(call_ivs.keys()) + list(put_ivs.keys())))
        if not all_strikes:
            continue

        # ── Surface rows (one per strike per expiry) ──
        for strike in all_strikes:
            call_iv = call_ivs.get(strike)
            put_iv  = put_ivs.get(strike)
            mid_iv  = None
            if call_iv is not None and put_iv is not None:
                mid_iv = round((call_iv + put_iv) / 2, 2)
            elif call_iv is not None:
                mid_iv = call_iv
            elif put_iv is not None:
                mid_iv = put_iv

            surface_rows.append({
                "expiry_date":    exp["date"],
                "days_to_expiry": round(T * 365),
                "strike":         strike,
                "moneyness":      round(strike / spot, 4),
                "call_iv":        call_iv,
                "put_iv":         put_iv,
                "mid_iv":         mid_iv,
            })

        # ── ATM IV for term structure ──
        atm_strike = min(all_strikes, key=lambda k: abs(k - spot))
        atm_iv = call_ivs.get(atm_strike) or put_ivs.get(atm_strike)
        if atm_iv is not None:
            term_structure.append({
                "expiry_date":    exp["date"],
                "days_to_expiry": round(T * 365),
                "atm_iv":         atm_iv,
            })

        # ── Skew for this expiry ──
        skew[exp["date"]] = [
            {
                "strike":    s,
                "moneyness": round(s / spot, 4),
                "call_iv":   call_ivs.get(s),
                "put_iv":    put_ivs.get(s),
            }
            for s in all_strikes
        ]

    return {
        "symbol":         symbol,
        "spot":           spot,
        "surface":        surface_rows,
        "term_structure": term_structure,
        "skew":           skew,
    }
