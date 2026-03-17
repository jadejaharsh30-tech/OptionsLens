# optionslens/backend/routers/ivrank.py
"""
GET /api/ivrank/{symbol}
Returns current ATM IV + IV Rank for a symbol.
IV Rank = (current_IV - 52w_low) / (52w_high - 52w_low) × 100

Requires historical IV snapshots from the daily scheduler.
Returns iv_rank=None with an explanatory note until enough history exists.
"""
from fastapi import APIRouter, Depends, HTTPException
from auth import get_token
from fyers_client import fetch_expiry_list, fetch_option_chain, fetch_quote, get_fyers
from iv_engine import implied_volatility
from snapshot_store import get_iv_rank
from config import UNDERLYINGS, RISK_FREE_RATE, DB_PATH
from routers.chain import days_to_expiry

router = APIRouter(prefix="/api/ivrank", tags=["ivrank"])


@router.get("/{symbol}")
def get_iv_rank_endpoint(symbol: str, token: str = Depends(get_token)):
    """
    Computes current ATM IV from live chain, then looks up IV Rank
    from local historical snapshot store.

    Returns:
        current_iv: ATM IV as % (e.g. 13.5 means 13.5%)
        iv_rank:    0-100 percentile rank, or null if insufficient history
        history_days: number of days of IV history available
        note:       explanation if iv_rank is null
    """
    symbol = symbol.upper()
    if symbol not in UNDERLYINGS:
        raise HTTPException(400, f"Unknown symbol: {symbol}")

    fyers    = get_fyers(token)
    spot     = fetch_quote(fyers, symbol)
    expiries = fetch_expiry_list(fyers, symbol)

    if not expiries:
        return {"symbol": symbol, "current_iv": None, "iv_rank": None,
                "note": "No expiry data available."}

    # Use nearest expiry with T > 0
    exp = next((e for e in expiries if days_to_expiry(e["date"]) > 0), None)
    if exp is None:
        return {"symbol": symbol, "current_iv": None, "iv_rank": None,
                "note": "All expiries have passed."}

    T     = days_to_expiry(exp["date"])
    chain = fetch_option_chain(fyers, symbol, exp["expiry"], strike_count=6)

    # Collect IV values for strikes within 2% of ATM
    iv_values = []
    for opt in chain:
        if opt["ltp"] <= 0:
            continue
        if abs(opt["strike"] - spot) / spot > 0.02:
            continue
        iv = implied_volatility(
            market_price=opt["ltp"],
            S=spot, K=opt["strike"], T=T,
            r=RISK_FREE_RATE,
            option_type=opt["option_type"],
        )
        if iv is not None:
            iv_values.append(iv)

    current_iv     = sum(iv_values) / len(iv_values) if iv_values else None
    current_iv_pct = round(current_iv * 100, 2) if current_iv else None

    iv_rank = get_iv_rank(DB_PATH, symbol, current_iv) if current_iv else None

    # How many days of history do we have?
    from snapshot_store import get_atm_iv_history
    history = get_atm_iv_history(DB_PATH, symbol, days=365)

    note = None
    if iv_rank is None:
        days_so_far = len(history)
        note = (
            f"IV Rank requires 5+ days of history. "
            f"Currently have {days_so_far} day(s). "
            f"Snapshot runs daily at 15:20 IST — check back tomorrow."
        )

    return {
        "symbol":       symbol,
        "spot":         spot,
        "expiry_date":  exp["date"],
        "current_iv":   current_iv_pct,
        "iv_rank":      iv_rank,
        "history_days": len(history),
        "note":         note,
    }
