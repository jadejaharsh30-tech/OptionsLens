# optionslens/backend/routers/ivrank.py
"""
GET /api/ivrank/{symbol}
Returns current ATM IV, IV Rank, and Realized Volatility comparison for a symbol.

IV Rank = (current_IV - 52w_low) / (52w_high - 52w_low) × 100
RV = close-to-close annualised realized volatility (20d and 60d windows)
Vol Premium = ATM IV - RV 20d (positive = IV rich, negative = IV cheap)

RV is computed from Fyers historical daily OHLC — available from day 1,
no local snapshot accumulation required.

IV Rank still requires local daily snapshots (no broker provides historical IV).
Returns iv_rank=None with an explanatory note until 5+ days of history exist.
"""
from fastapi import APIRouter, Depends, HTTPException
from auth import get_token
from fyers_client import (
    fetch_expiry_list, fetch_option_chain, fetch_quote,
    fetch_historical_prices, get_fyers,
)
from iv_engine import implied_volatility
from snapshot_store import get_iv_rank, get_atm_iv_history
from realized_vol import compute_realized_vol, compute_rv_series
from config import UNDERLYINGS, RISK_FREE_RATE, DB_PATH
from routers.chain import days_to_expiry

router = APIRouter(prefix="/api/ivrank", tags=["ivrank"])


@router.get("/{symbol}")
def get_iv_rank_endpoint(symbol: str, token: str = Depends(get_token)):
    """
    Computes current ATM IV from live chain.
    RV is fetched from Fyers historical daily prices — fully available from day 1.
    IV Rank is looked up from the local snapshot store — builds over time.

    Returns:
        current_iv:   ATM IV as % (e.g. 13.5 = 13.5%)
        iv_rank:      0-100 percentile rank, or null if < 5 days history
        history_days: days of local IV history available
        note:         explanation if iv_rank is null
        rv_20d:       20-day annualised RV as % — available immediately
        rv_60d:       60-day annualised RV as % — available immediately
        vol_premium:  ATM IV minus RV 20d in pct points (positive = IV rich)
        iv_rv_series: [{date, atm_iv, rv_20d, premium}] for the IVvsRV chart
    """
    symbol = symbol.upper()
    if symbol not in UNDERLYINGS:
        raise HTTPException(400, f"Unknown symbol: {symbol}")

    fyers    = get_fyers(token)
    spot     = fetch_quote(fyers, symbol)
    expiries = fetch_expiry_list(fyers, symbol)

    empty = {
        "symbol": symbol, "current_iv": None, "iv_rank": None,
        "note": None, "rv_20d": None, "rv_60d": None,
        "vol_premium": None, "iv_rv_series": [],
    }

    if not expiries:
        return {**empty, "note": "No expiry data available."}

    exp = next((e for e in expiries if days_to_expiry(e["date"]) > 0), None)
    if exp is None:
        return {**empty, "note": "All expiries have passed."}

    T     = days_to_expiry(exp["date"])
    chain = fetch_option_chain(fyers, symbol, exp["expiry"], strike_count=6)

    # ── ATM IV from live chain ────────────────────────────────────────────────
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

    # ── IV Rank from local snapshot store ────────────────────────────────────
    iv_rank = get_iv_rank(DB_PATH, symbol, current_iv) if current_iv else None
    history = get_atm_iv_history(DB_PATH, symbol, days=365)

    note = None
    if iv_rank is None:
        note = (
            f"IV Rank requires 5+ days of history. "
            f"Currently have {len(history)} day(s). "
            f"Snapshot runs daily at 15:20 IST — check back tomorrow."
        )

    # ── Realized vol from Fyers historical prices — available from day 1 ─────
    # Fetch 90 calendar days of daily closes (~63 trading days)
    closes = fetch_historical_prices(fyers, symbol, days=90)

    rv_20d_pct   = None
    rv_60d_pct   = None
    vol_premium  = None
    iv_rv_series = []

    if closes:
        rv_20d = compute_realized_vol(closes, window=20)
        rv_60d = compute_realized_vol(closes, window=60)
        rv_20d_pct = round(rv_20d * 100, 2) if rv_20d is not None else None
        rv_60d_pct = round(rv_60d * 100, 2) if rv_60d is not None else None

        if current_iv_pct is not None and rv_20d_pct is not None:
            vol_premium = round(current_iv_pct - rv_20d_pct, 2)

        # Build IV vs RV time series for the AreaChart
        rv_series   = compute_rv_series(closes, window=20)
        iv_hist_map = {r["date"]: round(r["iv"] * 100, 2) for r in history}

        if rv_series and iv_hist_map:
            # Aligned: dates present in both IV history and RV series
            iv_rv_series = [
                {
                    "date":    e["date"],
                    "atm_iv":  iv_hist_map[e["date"]],
                    "rv_20d":  round(e["rv"] * 100, 2),
                    "premium": round(iv_hist_map[e["date"]] - e["rv"] * 100, 2),
                }
                for e in rv_series
                if e["date"] in iv_hist_map
            ]
        elif rv_series:
            # RV only — IV history not yet built. Return RV series alone.
            # IVvsRVPanel handles atm_iv=None gracefully.
            iv_rv_series = [
                {
                    "date":    e["date"],
                    "atm_iv":  None,
                    "rv_20d":  round(e["rv"] * 100, 2),
                    "premium": None,
                }
                for e in rv_series
            ]

    return {
        "symbol":       symbol,
        "spot":         spot,
        "expiry_date":  exp["date"],
        "current_iv":   current_iv_pct,
        "iv_rank":      iv_rank,
        "history_days": len(history),
        "note":         note,
        "rv_20d":       rv_20d_pct,
        "rv_60d":       rv_60d_pct,
        "vol_premium":  vol_premium,
        "iv_rv_series": iv_rv_series,
    }
