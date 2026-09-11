# optionslens/backend/market_hours.py
"""
Single source of truth for NSE session timing.

CAS (Closing Auction Session) went live on 3 August 2026 and changed the shape
of the trading day for F&O-eligible cash stocks:

    09:15  continuous trading opens
    15:15  continuous trading ENDS for F&O-eligible cash stocks
    15:15  closing auction begins (order collection, single equilibrium price)
    15:35  auction price finalised (randomised close, published ~15:30-15:35)
    15:40  derivatives (futures & options) stop trading

Consequences this module exists to prevent:
  - Treating 15:30 as "market closed" and stopping data capture while options
    are still trading until ~15:40.
  - Sampling a stock's "spot" between 15:15 and 15:35 and calling it a closing
    price — there is no continuous trading then, so the print is stale.

NOTE: published secondary sources disagree on whether the auction window ends
at 15:30 or 15:35. The conservative choice is made here (treat the window as
running to 15:35 and keep recording to 15:40). Verify against the live NSE
circular before relying on these minutes in production.
"""
from datetime import date, datetime, time
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# ── Session boundaries (IST) ──────────────────────────────────────────────────
PRE_OPEN_START        = time(9,  0)
CONTINUOUS_OPEN       = time(9,  15)
CONTINUOUS_CASH_CLOSE = time(15, 15)   # CAS: cash trading stops here for F&O stocks
CAS_WINDOW_END        = time(15, 35)   # auction equilibrium price finalised
DERIVATIVES_CLOSE     = time(15, 40)   # options/futures stop trading

# The moment an expiring contract stops having time value. Index options have
# conventionally expired at 15:30 IST; CAS did not move this, but confirm
# against the NSE circular if settlement timing is ever material to a signal.
EXPIRY_TIME_IST = time(15, 30)

SECONDS_PER_YEAR = 365.0 * 24 * 3600

# Exchange holidays (YYYY-MM-DD). Extend as needed — weekends are handled
# automatically. An incomplete list only costs us empty polls, never bad data.
NSE_HOLIDAYS: set[str] = set()


class SessionPhase(str, Enum):
    """Where we are in the trading day. String-valued so it persists directly."""
    CLOSED      = "CLOSED"        # outside any session, weekend, or holiday
    PRE_OPEN    = "PRE_OPEN"      # 09:00-09:15 pre-open call auction
    CONTINUOUS  = "CONTINUOUS"    # 09:15-15:15 normal continuous trading
    CAS_WINDOW  = "CAS_WINDOW"    # 15:15-15:35 closing auction; cash stocks frozen
    POST_CAS    = "POST_CAS"      # 15:35-15:40 derivatives still trading


def now_ist() -> datetime:
    """Current time in IST. Always use this rather than datetime.now()."""
    return datetime.now(IST)


def is_trading_day(d: Optional[date] = None) -> bool:
    """True on weekdays that are not listed exchange holidays."""
    d = d or now_ist().date()
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in NSE_HOLIDAYS


def get_session_phase(now: Optional[datetime] = None) -> SessionPhase:
    """
    Classify the current moment into a session phase.

    Every recorded snapshot is tagged with this, because the same raw fields
    mean different things in different phases: an unchanged LTP during
    CAS_WINDOW is a frozen book, not a quiet market.
    """
    now = now or now_ist()
    if not is_trading_day(now.date()):
        return SessionPhase.CLOSED

    t = now.time()
    if t < PRE_OPEN_START:
        return SessionPhase.CLOSED
    if t < CONTINUOUS_OPEN:
        return SessionPhase.PRE_OPEN
    if t < CONTINUOUS_CASH_CLOSE:
        return SessionPhase.CONTINUOUS
    if t < CAS_WINDOW_END:
        return SessionPhase.CAS_WINDOW
    if t < DERIVATIVES_CLOSE:
        return SessionPhase.POST_CAS
    return SessionPhase.CLOSED


def is_derivatives_open(now: Optional[datetime] = None) -> bool:
    """
    True while options/futures can still trade (09:15 - 15:40).

    This is the correct gate for anything that polls the option chain.
    The old `is_market_open()` 15:30 cutoff silently dropped the final
    ~10 minutes of derivatives trading, which under CAS is exactly when
    hedging flow around the auction shows up.
    """
    return get_session_phase(now) in (
        SessionPhase.CONTINUOUS,
        SessionPhase.CAS_WINDOW,
        SessionPhase.POST_CAS,
    )


def is_continuous_session(now: Optional[datetime] = None) -> bool:
    """True only during 09:15-15:15 continuous trading."""
    return get_session_phase(now) is SessionPhase.CONTINUOUS


def is_recording_window(now: Optional[datetime] = None) -> bool:
    """
    True while the chain recorder should be capturing.

    Deliberately wider than `is_continuous_session`: the CAS window and the
    post-CAS derivatives tail are the most interesting and least-studied part
    of the day, so we record straight through them.
    """
    return is_derivatives_open(now)


def is_cash_price_reliable(now: Optional[datetime] = None) -> bool:
    """
    False during the closing auction, when F&O-eligible cash stocks have no
    continuous trading and any quoted LTP is a stale pre-auction print.

    Guard every "closing spot price" capture with this. The daily IV snapshot
    at 15:20 IST currently violates it.
    """
    return get_session_phase(now) not in (SessionPhase.CAS_WINDOW, SessionPhase.CLOSED)


def time_to_expiry(expiry_date_str: str, now: Optional[datetime] = None) -> float:
    """
    Years to expiry, measured to the actual expiry instant (15:30 IST).

    Replaces the old whole-day `days_to_expiry`, which computed
    `(expiry - today).days / 365` and therefore returned exactly 0.0 all through
    expiry day. That single value silently disabled the tool when it mattered
    most: `chain.py` skipped IV and Greeks for the entire chain, `surface.py`
    dropped the expiry, and `ivrank.py` quietly rolled to the next expiry.
    0DTE gamma — the most interesting case for the GEX module — was unreachable.

    Also fixes a latent timezone bug: the old version used `date.today()`, which
    is the server's local date. On a UTC host (any Docker deploy) that is the
    wrong calendar day for an IST market during the evening.

    Args:
        expiry_date_str: Fyers expiry string, DD-MM-YYYY (e.g. "17-09-2026").
        now: override for testing; defaults to current IST time.

    Returns:
        Time to expiry in years, or 0.0 once expired. Sub-minute remainders are
        treated as expired — there is no tradeable time value left, and feeding
        a near-zero T into the Black-Scholes solver only produces noise.
    """
    try:
        expiry_day = datetime.strptime(expiry_date_str, "%d-%m-%Y").date()
    except ValueError:
        # Fyers has been seen returning DD-Mon-YYYY on some instruments.
        expiry_day = datetime.strptime(expiry_date_str, "%d-%b-%Y").date()

    expiry_dt = datetime.combine(expiry_day, EXPIRY_TIME_IST, tzinfo=IST)
    now = now or now_ist()

    remaining = (expiry_dt - now).total_seconds()
    if remaining <= 60:
        return 0.0
    return remaining / SECONDS_PER_YEAR


def days_to_expiry(expiry_date_str: str, now: Optional[datetime] = None) -> float:
    """Deprecated alias for `time_to_expiry`, kept for existing call sites."""
    return time_to_expiry(expiry_date_str, now)


def seconds_until_next_tick(interval_sec: int, now: Optional[datetime] = None) -> float:
    """
    Seconds to sleep so polls land on clean interval boundaries.

    Aligned timestamps make snapshots directly joinable across symbols and
    resampleable into bars without interpolation.
    """
    now = now or now_ist()
    secs_into_day = (
        now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
    )
    remainder = interval_sec - (secs_into_day % interval_sec)
    return max(0.1, remainder)
