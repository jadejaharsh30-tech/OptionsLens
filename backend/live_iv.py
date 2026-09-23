# optionslens/backend/live_iv.py
"""
30-day constant-maturity ATM IV from live Fyers chains.

Shared by /api/ivrank and the 15:10 daily snapshot so the live reading and the
stored history are built the same way: the same expiry choice, the same ATM
definition (chain_pricing.atm_iv_for_chain), the same interpolation. IV Rank
compares one against the other, so any difference between them would read as a
move in volatility.

Fetches only the chains the interpolation needs, normally two: the expiries
either side of 30 days. Not every listed expiry, which on NIFTY weeklies would
be a dozen chain calls per request.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from chain_pricing import atm_iv_for_chain
from eod_vol import DEFAULT_TARGET_DAYS, constant_maturity_iv, expiries_for_tenor
from fyers_client import fetch_option_chain
from market_hours import time_to_expiry


@dataclass
class ExpiryReading:
    expiry_date: str            # DD-MM-YYYY, as Fyers reports it
    tenor_days: float
    atm_iv: Optional[float]     # decimal; None when the chain would not solve
    chain: list[dict] = field(default_factory=list, repr=False)


@dataclass
class TermReading:
    cm_iv: Optional[float]                  # decimal, at `target_days`
    target_days: float
    expiries: list[ExpiryReading]

    @property
    def expiries_used(self) -> list[str]:
        return [e.expiry_date for e in self.expiries if e.atm_iv is not None]


def live_cm_atm_iv(fyers, symbol: str, spot: float, expiries: list[dict],
                   target_days: float = DEFAULT_TARGET_DAYS,
                   strike_count: int = 6,
                   now: Optional[datetime] = None) -> TermReading:
    """
    Fetch the chains bracketing `target_days` and interpolate their ATM IVs.

    `expiries` is fetch_expiry_list output: [{"expiry": epoch, "date": "DD-MM-YYYY"}].
    """
    tenors = [time_to_expiry(e["date"], now) * 365.0 for e in expiries]
    readings = []
    for i in expiries_for_tenor(tenors, target_days):
        exp = expiries[i]
        T = tenors[i] / 365.0
        chain = fetch_option_chain(fyers, symbol, exp["expiry"], strike_count=strike_count)
        readings.append(ExpiryReading(
            expiry_date=exp["date"], tenor_days=tenors[i],
            atm_iv=atm_iv_for_chain(chain, T, spot) if chain else None,
            chain=chain or [],
        ))

    points = [(r.tenor_days, r.atm_iv) for r in readings if r.atm_iv is not None]
    return TermReading(cm_iv=constant_maturity_iv(points, target_days),
                       target_days=target_days, expiries=readings)
