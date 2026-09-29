# optionslens/backend/futures.py
"""
Index and stock futures: which contract is the front month, what Fyers calls
it, and what its price says about carry (roadmap item 15).

WHY FUTURES ARE WORTH RECORDING AT ALL
    Option IV is already priced off a forward implied from put-call parity, so
    futures are not needed to price options. They are needed for three other
    things:

    1. **The CAS window.** From 15:15 to 15:35 an F&O-eligible stock's cash
       book is frozen in the closing auction, and the index built from those
       stocks is frozen with it. Futures keep trading continuously. For those
       twenty minutes the future is the only live price of the underlying, and
       it is what separates "the market moved while the auction ran" from "the
       auction printed away from where derivatives priced it" (item 34).
    2. **Basis.** ln(F/S)/T is the market's implied carry — the funding rate
       minus the dividend yield. It moves with positioning and funding stress,
       and it is a second, independent read on the forward that the option
       chain's own parity recovers.
    3. **A check on the implied forward.** For the monthly expiry, the forward
       recovered from put-call parity and the traded future are the same
       quantity. A persistent gap between them means the forward recovery is
       wrong, which would contaminate every IV this app computes.

WHICH CONTRACT
    NSE lists futures monthly only; there are no weekly futures. The front
    month is found from the OPTION expiry calendar the recorder already fetches:
    monthly options and futures share an expiry, and the monthly option expiry
    is the last listed expiry in its calendar month. That uses the exchange's
    own calendar rather than a weekday rule, which matters — NSE moved monthly
    expiry from Thursday to Tuesday in 2025, and any hardcoded rule written
    before that would now silently pick the wrong contract.

THE FYERS SYMBOL FORMAT IS AN ASSUMPTION UNTIL A LIVE RUN CONFIRMS IT
    `NSE:NIFTY26OCTFUT` — exchange, NSE trading symbol, two-digit year,
    three-letter month, `FUT`. This follows Fyers' documented convention but has
    not been checked against a live response from this codebase. The recorder
    therefore counts futures captured versus missed and exposes both on its
    status endpoint: a wrong format shows up as a zero, not as a silent absence.

Pure functions over plain values. No broker, no database, except the one EOD
loader at the bottom, which reads the bhavcopy archive.
"""
import math
import sqlite3
from datetime import date, datetime
from typing import Iterable, Optional

from market_hours import EXPIRY_TIME_IST, IST, time_to_expiry

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _parse_fyers_date(s: str) -> Optional[date]:
    """Fyers dates are DD-MM-YYYY; tolerate the DD-Mon-YYYY form seen on some instruments."""
    for fmt in ("%d-%m-%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def monthly_expiries(expiry_dates: Iterable[date]) -> list[date]:
    """
    The last listed expiry of each calendar month, ascending.

    On an index that is the monthly contract, which futures share; on a stock,
    whose options are monthly only, every listed expiry already is one.
    """
    last_in_month: dict[tuple[int, int], date] = {}
    for d in expiry_dates:
        if d is None:
            continue
        key = (d.year, d.month)
        if key not in last_in_month or d > last_in_month[key]:
            last_in_month[key] = d
    return sorted(last_in_month.values())


def front_month_expiry(option_expiries: Iterable[str],
                       now: Optional[datetime] = None) -> Optional[date]:
    """
    Expiry date of the front-month future, from the OPTION expiry list.

    `option_expiries` are Fyers DD-MM-YYYY strings — exactly what
    `fetch_expiry_list` returns as `date`. The front month is the earliest
    monthly expiry with time left; on expiry day that is the expiring contract
    until 15:30, then the next one, because a contract past its expiry instant
    quotes a settlement rather than a market.
    """
    parsed = [_parse_fyers_date(s) for s in option_expiries]
    for d in monthly_expiries(p for p in parsed if p is not None):
        if time_to_expiry(d.strftime("%d-%m-%Y"), now=now) > 0:
            return d
    return None


def fyers_future_symbol(symbol_key: str, expiry: date) -> str:
    """
    Fyers symbol for a monthly future, e.g. NIFTY + 2026-10-27 -> NSE:NIFTY26OCTFUT.

    `symbol_key` is the NSE trading symbol, which is what the keys of
    `config.UNDERLYINGS` are. Note it is NOT the index's spot symbol
    (`NSE:NIFTY50-INDEX`): futures are listed under the derivative name.
    """
    return f"NSE:{symbol_key.upper()}{expiry.year % 100:02d}{_MONTHS[expiry.month - 1]}FUT"


def implied_carry(futures: Optional[float], spot: Optional[float],
                  T: float) -> Optional[float]:
    """
    Annualised implied carry, ln(F/S)/T, as a decimal (0.055 = 5.5% a year).

    Under cost of carry F = S·e^((r−q)T), so this is the funding rate net of
    the dividend yield the market is pricing. Continuous compounding, because
    that is the convention the Black-76 forward uses — mixing it with a simple
    rate would put a small, T-dependent wedge between the two forwards that
    reads as a disagreement when it is only arithmetic.

    None below a day to expiry: dividing a few points of basis by a T near zero
    produces an annualised number in the hundreds of percent that means nothing.
    """
    if not futures or not spot or futures <= 0 or spot <= 0:
        return None
    if T < 1.0 / 365.0:
        return None
    return math.log(futures / spot) / T


def _close_instant(trad_dt: str) -> datetime:
    d = datetime.strptime(trad_dt, "%Y-%m-%d").date()
    return datetime.combine(d, EXPIRY_TIME_IST, tzinfo=IST)


def load_basis_history(symbol: str, src_db: Optional[str] = None,
                       min_volume: float = 1.0) -> list[dict]:
    """
    Front-month futures basis per trading date, from the bhavcopy archive.

    Reads `daily_future`, which `bhavcopy.download` has been storing all along
    without anything consuming it. One row per date: the nearest contract that
    has not yet expired (`expiry_dt > trad_dt`, the same rule the importer
    applies to options, because on expiry day the settlement column carries the
    underlying's level rather than a price).

    Only TRADED contracts are used. An untraded contract republishes its
    previous close, and a basis computed from yesterday's future against today's
    spot is a day's return mislabelled as carry.

    Dates whose file publishes no underlying price (pre-2024-07-08) are skipped:
    backing spot out of the future would need the very carry being measured.

    Returns [{"date", "spot", "futures", "expiry", "days", "basis_pts",
    "carry"}] ascending; `carry` is the annualised implied carry as a decimal.
    """
    from config import NSE_EOD_DB

    conn = sqlite3.connect(src_db or NSE_EOD_DB)
    try:
        rows = conn.execute("""
            SELECT trad_dt, expiry_dt, close, volume, underlying
            FROM daily_future
            WHERE symbol = ? AND expiry_dt > trad_dt
            ORDER BY trad_dt, expiry_dt
        """, (symbol.upper(),)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    front: dict[str, tuple] = {}
    for trad_dt, expiry_dt, close, volume, underlying in rows:
        if trad_dt in front:
            continue                        # ordered by expiry: first one wins
        if not close or close <= 0 or not volume or volume < min_volume:
            continue
        front[trad_dt] = (expiry_dt, close, underlying)

    out = []
    for trad_dt in sorted(front):
        expiry_dt, fut, spot = front[trad_dt]
        if not spot or spot <= 0:
            continue
        fyers_expiry = datetime.strptime(expiry_dt, "%Y-%m-%d").strftime("%d-%m-%Y")
        T = time_to_expiry(fyers_expiry, now=_close_instant(trad_dt))
        carry = implied_carry(fut, spot, T)
        out.append({
            "date":      trad_dt,
            "spot":      spot,
            "futures":   fut,
            "expiry":    expiry_dt,
            "days":      round(T * 365.0, 3),
            "basis_pts": round(fut - spot, 4),
            "carry":     None if carry is None else round(carry, 6),
        })
    return out


def summarise_basis(series: list[dict]) -> dict:
    """
    Diagnostics for the report and the API.

    The carry figure worth sanity-checking is the mean against the configured
    risk-free rate: on an index their difference is roughly the dividend yield,
    so a mean carry ABOVE the risk-free rate, or several points below it, says
    the rows are being paired wrongly rather than that funding is unusual.
    """
    carries = [r["carry"] for r in series if r.get("carry") is not None]
    if not series:
        return {"observations": 0,
                "note": ("No front-month futures with a published underlying. "
                         "Has bhavcopy.download run, and are the files from "
                         "2024-07-08 or later?")}
    return {
        "observations": len(series),
        "first_date":   series[0]["date"],
        "last_date":    series[-1]["date"],
        "latest_basis_pts": series[-1]["basis_pts"],
        "mean_carry_pct": round(sum(carries) / len(carries) * 100, 3) if carries else None,
        "pct_backwardated": round(sum(1 for r in series if r["basis_pts"] < 0)
                                  / len(series) * 100, 1),
    }


def _report() -> None:
    """`python -m futures` — basis coverage per configured underlying."""
    from config import RISK_FREE_RATE, UNDERLYINGS

    print(f"{'symbol':<12}{'dates':>7}{'mean carry':>12}{'backwardated':>14}  window")
    for symbol in UNDERLYINGS:
        info = summarise_basis(load_basis_history(symbol))
        if not info["observations"]:
            print(f"{symbol:<12}{0:>7}{'-':>12}{'-':>14}  -")
            continue
        carry = (f"{info['mean_carry_pct']:.2f}%"
                 if info["mean_carry_pct"] is not None else "-")
        print(f"{symbol:<12}{info['observations']:>7}{carry:>12}"
              f"{info['pct_backwardated']:>13.1f}%  "
              f"{info['first_date']} to {info['last_date']}")
    print(f"\nRisk-free rate in config: {RISK_FREE_RATE * 100:.2f}%. Mean carry "
          f"should sit below it by roughly the dividend yield;\nabove it, or far "
          f"below, means rows are being paired wrongly.")


if __name__ == "__main__":
    _report()
