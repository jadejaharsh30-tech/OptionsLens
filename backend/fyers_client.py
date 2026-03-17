# optionslens/backend/fyers_client.py
"""
Thin wrapper around Fyers SDK.
One FyersModel instance per request (stateless — token passed each time).

NOTE: Fyers returns expiry dates in DD-MM-YYYY format (e.g. "24-04-2025").
      All date parsing in this project uses "%d-%m-%Y".
"""
from fyers_apiv3 import fyersModel
from config import UNDERLYINGS

FYERS_CLIENT_ID = "P0SR1BM6RF-100"  # Your app client ID


def get_fyers(token: str):
    """Return a FyersModel instance for the given access token."""
    return fyersModel.FyersModel(
        client_id=FYERS_CLIENT_ID,
        token=token,
        log_path=None,
    )


def fetch_quote(fyers, symbol_key: str) -> float:
    """
    Fetch current spot price for an underlying.
    Returns LTP (last traded price).
    Raises ValueError on API error.
    """
    cfg  = UNDERLYINGS[symbol_key]
    resp = fyers.quotes({"symbols": cfg["symbol"]})
    if resp.get("s") == "error":
        raise ValueError(f"Fyers quote error for {symbol_key}: {resp}")
    return resp["d"][0]["v"]["lp"]


def fetch_expiry_list(fyers, symbol_key: str) -> list[dict]:
    """
    Fetch all available expiries for an underlying.
    Returns list of {expiry: epoch_int, date: 'DD-MM-YYYY'}.
    """
    cfg  = UNDERLYINGS[symbol_key]
    resp = fyers.optionchain({"symbol": cfg["symbol"], "strikecount": 1, "timestamp": ""})
    expiry_data = resp.get("data", {}).get("expiryData", [])
    if not expiry_data:
        raise ValueError(f"No expiryData returned for {symbol_key}. Check token.")
    return expiry_data  # [{expiry: int, date: "DD-MM-YYYY"}, ...]


def fetch_option_chain(fyers, symbol_key: str, expiry_epoch: int,
                       strike_count: int = 20) -> list[dict]:
    """
    Fetch full option chain for a given expiry.
    Returns normalised list of option rows.

    Fyers response fields used:
        strike_price, option_type, oi, oich, oichp, prev_oi, ltp, volume
    """
    cfg  = UNDERLYINGS[symbol_key]
    resp = fyers.optionchain({
        "symbol":      cfg["symbol"],
        "strikecount": strike_count,
        "timestamp":   expiry_epoch,
    })
    if "data" not in resp or "optionsChain" not in resp.get("data", {}):
        raise ValueError(f"Bad optionchain response for {symbol_key}: {resp}")

    chain = []
    for opt in resp["data"]["optionsChain"]:
        if opt.get("strike_price", -1) == -1:
            continue
        if opt.get("option_type") not in ("CE", "PE"):
            continue
        chain.append({
            "strike":        opt["strike_price"],
            "option_type":   opt["option_type"],
            "oi":            opt.get("oi", 0),
            "oi_change":     opt.get("oich", 0),
            "oi_change_pct": opt.get("oichp", 0),
            "prev_oi":       opt.get("prev_oi", 0),
            "ltp":           opt.get("ltp", 0),
            "volume":        opt.get("volume", 0),
            "bid":           opt.get("bid", 0),
            "ask":           opt.get("ask", 0),
        })
    return chain
