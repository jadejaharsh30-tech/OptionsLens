# optionslens/backend/routers/expiries.py
"""
GET /api/expiries/{symbol}
Returns all available expiry dates + epochs for a given underlying.
This is always the first call the frontend makes — expiry selection
feeds into every other endpoint.
"""
from fastapi import APIRouter, Depends, HTTPException
from auth import get_token
from fyers_client import fetch_expiry_list, get_fyers
from config import UNDERLYINGS

router = APIRouter(prefix="/api/expiries", tags=["expiries"])


@router.get("/{symbol}")
def get_expiries(symbol: str, token: str = Depends(get_token)):
    """
    Returns list of {expiry: epoch_int, date: 'DD-MM-YYYY'} for the symbol.
    Frontend uses 'expiry' (epoch) as the key for all subsequent chain calls.
    """
    symbol = symbol.upper()
    if symbol not in UNDERLYINGS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown symbol: {symbol}. Valid symbols: {list(UNDERLYINGS.keys())}"
        )
    fyers = get_fyers(token)
    return {
        "symbol":   symbol,
        "expiries": fetch_expiry_list(fyers, symbol),
    }
