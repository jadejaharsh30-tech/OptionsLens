# optionslens/backend/routers/surface.py
"""
GET /api/surface/{symbol}?interpolate=false
Fetches IV surface across all available expiries.
Returns data for three frontend charts:
  1. 3D IV surface   (strike × days_to_expiry × IV)
  2. Skew curves     (per expiry: strike × call_iv / put_iv)
  3. Term structure  (ATM IV across expiries)

Optional ?interpolate=true fills surface gaps using SVI parametric fitting.

IVs are solved with Black-76 against a forward implied per expiry from the
chain's own put-call parity, so a call/put IV gap at one strike is real skew
rather than the dividend-yield artefact spot-based pricing produces.
"""
import math
from fastapi import APIRouter, Depends, HTTPException, Query
from auth import get_token
from chain_pricing import implied_forward_for_chain, price_for_iv
from fyers_client import fetch_expiry_list, fetch_option_chain, fetch_quote, get_fyers
from iv_engine import implied_vol_forward
from svi_engine import interpolate_surface
from config import UNDERLYINGS, RISK_FREE_RATE
from market_hours import time_to_expiry as days_to_expiry

router = APIRouter(prefix="/api/surface", tags=["surface"])

# Max expiries to fetch — keeps response time reasonable
MAX_EXPIRIES = 6


@router.get("/{symbol}")
def get_iv_surface(
    symbol:      str,
    interpolate: bool = Query(False, description="Fill surface gaps using SVI interpolation"),
    token:       str  = Depends(get_token),
):
    """
    Returns IV surface data across up to 6 expiries.

    Response shape:
    {
      symbol, spot,
      surface: [{expiry_date, days_to_expiry, strike, moneyness,
                 call_iv, put_iv, mid_iv, interpolated}],
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

    surface_rows   = []
    term_structure = []
    skew: dict     = {}

    for exp in expiries[:MAX_EXPIRIES]:
        T = days_to_expiry(exp["date"])
        if T <= 0:
            continue

        chain = fetch_option_chain(fyers, symbol, exp["expiry"], strike_count=16)

        # One forward per expiry slice. Calls and puts at a strike then share it,
        # so any residual call/put IV gap is genuine market skew rather than the
        # dividend-yield artefact that spot-based pricing manufactures.
        forward = implied_forward_for_chain(chain, T, spot)
        if forward is None:
            continue

        call_ivs: dict[float, float] = {}
        put_ivs:  dict[float, float] = {}

        for opt in chain:
            price = price_for_iv(opt)
            if not price:
                continue
            iv = implied_vol_forward(
                market_price=price,
                F=forward,
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
                # Log-moneyness against the FORWARD is the correct x-axis for
                # skew and the natural coordinate for SVI (which parameterises
                # total variance in k = ln(K/F)).
                "log_moneyness":  round(math.log(strike / forward), 6),
                "call_iv":        call_iv,
                "put_iv":         put_iv,
                "mid_iv":         mid_iv,
                "interpolated":   False,  # raw market data
            })

        # ── ATM IV for term structure ──
        atm_strike = min(all_strikes, key=lambda k: abs(k - spot))
        atm_iv = call_ivs.get(atm_strike) or put_ivs.get(atm_strike)
        if atm_iv is not None:
            term_structure.append({
                "expiry_date":    exp["date"],
                "days_to_expiry": round(T * 365),
                "atm_iv":         atm_iv,
                "forward":        round(forward, 2),
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

    # ── Optional SVI gap-filling ──────────────────────────────────────────────
    if interpolate and surface_rows:
        extra_rows = interpolate_surface(surface_rows, spot)
        surface_rows = surface_rows + extra_rows

    return {
        "symbol":         symbol,
        "spot":           spot,
        "surface":        surface_rows,
        "term_structure": term_structure,
        "skew":           skew,
    }
