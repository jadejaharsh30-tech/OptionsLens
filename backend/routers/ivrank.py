# optionslens/backend/routers/ivrank.py
"""
GET /api/ivrank/{symbol}
Returns current ATM IV, IV Rank, and Realized Volatility comparison for a symbol.

Current IV is 30-day constant-maturity ATM IV: the ATM IVs of the two expiries
either side of 30 days, interpolated in total variance. It used to be the
nearest expiry's ATM IV, which on NIFTY weeklies swings several vol points
purely from the contract approaching expiry, so the rank largely tracked the
day of the expiry cycle.

IV Rank is the percentile of that reading within the last 252 dated 30-day
readings in the local store — the share of past readings below today's. It is
no longer (IV − low) / (high − low), where one spike day set the range for a
year and pinned every later reading near zero.

RV = close-to-close annualised realized volatility (20d and 60d windows)
Vol Premium = 30-day ATM IV − RV 20d (positive = IV rich, negative = IV cheap).
20 trading days is roughly 30 calendar days, so the two legs now share a tenor.

History comes from the 15:10 daily snapshot and, for anything before it, from
exchange EOD data loaded with `python -m bhavcopy.importer`.
"""
from fastapi import APIRouter, Depends, HTTPException
from auth import get_token
from fyers_client import (
    fetch_expiry_list, fetch_quote, fetch_historical_prices, get_fyers,
)
from live_iv import live_cm_atm_iv
from snapshot_store import get_cm_iv_history, get_iv_percentile, get_spot_history
from realized_vol import compute_realized_vol, rv_series_from_dated_closes
from config import UNDERLYINGS, DB_PATH

HISTORY_DATES = 252      # one year of trading dates

router = APIRouter(prefix="/api/ivrank", tags=["ivrank"])


@router.get("/{symbol}")
def get_iv_rank_endpoint(symbol: str, token: str = Depends(get_token)):
    """
    Computes current 30-day ATM IV from the live chains either side of 30 days.
    RV is fetched from Fyers historical daily prices — fully available from day 1.
    IV Rank is a percentile against the local 30-day IV history.

    Returns:
        current_iv:    30-day constant-maturity ATM IV as % (13.5 = 13.5%)
        iv_rank:       0-100 percentile of current_iv in the last 252 dated
                       readings, or null with fewer than 20
        rank_method:   "percentile"
        iv_tenor_days: 30
        expiries_used: expiries whose chains produced current_iv
        history_days:  dates with a 30-day reading in the local store
        note:          explanation when current_iv or iv_rank is null
        rv_20d:        20-day annualised RV as % — available immediately
        rv_60d:        60-day annualised RV as % — available immediately
        vol_premium:   30-day ATM IV minus RV 20d in pct points
        iv_rv_series:  [{date, atm_iv, rv_20d, premium}] for the IVvsRV chart
    """
    symbol = symbol.upper()
    if symbol not in UNDERLYINGS:
        raise HTTPException(400, f"Unknown symbol: {symbol}")

    fyers    = get_fyers(token)
    spot     = fetch_quote(fyers, symbol)
    expiries = fetch_expiry_list(fyers, symbol)

    empty = {
        "symbol": symbol, "current_iv": None, "iv_rank": None,
        "rank_method": "percentile", "iv_tenor_days": 30, "expiries_used": [],
        "history_days": 0, "note": None, "rv_20d": None, "rv_60d": None,
        "vol_premium": None, "iv_rv_series": [],
    }

    if not expiries:
        return {**empty, "note": "No expiry data available."}

    # ── 30-day ATM IV from the live chains bracketing 30 days ─────────────────
    # Same expiry choice, ATM definition and interpolation as the daily
    # snapshot that builds the history, so rank compares like with like.
    term = live_cm_atm_iv(fyers, symbol, spot, expiries)
    current_iv     = term.cm_iv
    current_iv_pct = round(current_iv * 100, 2) if current_iv else None

    # ── IV Rank: percentile within the local 30-day history ───────────────────
    history = get_cm_iv_history(DB_PATH, symbol, days=HISTORY_DATES)
    iv_rank = get_iv_percentile(DB_PATH, symbol, current_iv, days=HISTORY_DATES) \
        if current_iv else None
    if iv_rank is not None:
        iv_rank = round(iv_rank, 2)

    note = None
    if current_iv is None:
        note = ("No 30-day IV right now: the expiries either side of 30 days did "
                "not both produce a solvable at-the-money quote.")
    elif iv_rank is None:
        note = (
            f"IV Rank needs 20+ days of 30-day IV history; the store has "
            f"{len(history)}. Load exchange history with "
            f"`python -m bhavcopy.importer`, or it builds by one day at 15:10 IST "
            f"each trading day."
        )

    # ── Realized vol from Fyers historical prices — available from day 1 ─────
    # Fetch 90 calendar days of daily closes (~63 trading days)
    closes = fetch_historical_prices(fyers, symbol, days=90)

    rv_20d_pct   = None
    rv_60d_pct   = None
    vol_premium  = None
    iv_rv_series = []
    rv_dates_exact = False

    if closes:
        rv_20d = compute_realized_vol(closes, window=20)
        rv_60d = compute_realized_vol(closes, window=60)
        rv_20d_pct = round(rv_20d * 100, 2) if rv_20d is not None else None
        rv_60d_pct = round(rv_60d * 100, 2) if rv_60d is not None else None

        if current_iv_pct is not None and rv_20d_pct is not None:
            vol_premium = round(current_iv_pct - rv_20d_pct, 2)

        # Build IV vs RV time series for the AreaChart.
        #
        # RV dates come from `spot_history`, which holds exchange closes with
        # their real dates. The previous version reconstructed dates by counting
        # weekdays backwards from today, which ignores exchange holidays and so
        # shifted the whole RV series against the IV series it is paired with
        # after every one of them. Falls back to the broker's undated closes
        # only when spot_history is empty, and says so in the response.
        dated = get_spot_history(DB_PATH, symbol, days=120)
        rv_series = rv_series_from_dated_closes(
            sorted(dated, key=lambda r: r["date"]), window=20)
        rv_dates_exact = bool(rv_series)
        if not rv_series:
            rv_series = []
        # One constant-maturity reading per date. Keying the raw per-expiry
        # rows by date instead would keep whichever expiry happened to be read
        # last, a different tenor on different days.
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
        "symbol":        symbol,
        "spot":          spot,
        "current_iv":    current_iv_pct,
        "iv_rank":       iv_rank,
        "rank_method":   "percentile",
        "iv_tenor_days": 30,
        "expiries_used": term.expiries_used,
        "history_days":  len(history),
        "note":          note,
        "rv_20d":        rv_20d_pct,
        "rv_60d":        rv_60d_pct,
        "vol_premium":   vol_premium,
        "iv_rv_series":  iv_rv_series,
        # False means spot_history had no closes and the chart is empty rather
        # than silently misaligned.
        "rv_dates_exact": rv_dates_exact,
    }
