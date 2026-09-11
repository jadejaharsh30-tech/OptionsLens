# optionslens/backend/recorder/models.py
"""
The data contract between live capture, storage, and backtest replay.

`ChainSnapshot` is the single interface every signal consumes. The recorder
produces them from Fyers; the backtester replays identical objects from disk.
A signal function must never be able to tell which one it is running against.

Design rule: store RAW broker fields only. IV, Greeks, GEX and every other
derived quantity are computed at read time, so that fixing the pricing model
(forward-based IV, better solver) retroactively improves all historical data
instead of invalidating it.
"""
from dataclasses import dataclass, field, asdict
from typing import Optional

from market_hours import SessionPhase


@dataclass(frozen=True)
class ChainRow:
    """One option contract at one instant. Raw fields exactly as Fyers returns them."""
    strike:        float
    option_type:   str            # "CE" or "PE"
    oi:            float = 0.0
    oi_change:     float = 0.0    # Fyers 'oich'  — Δ vs prior settlement
    oi_change_pct: float = 0.0    # Fyers 'oichp' — Δ% vs prior settlement
    prev_oi:       float = 0.0
    ltp:           float = 0.0
    bid:           float = 0.0
    ask:           float = 0.0
    volume:        float = 0.0

    @property
    def mid(self) -> Optional[float]:
        """Bid-ask mid, or None when the book is one-sided/empty.

        Preferred over LTP for IV solving: LTP can be minutes stale on illiquid
        strikes, which shows up as phantom IV spikes in the surface.
        """
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return None

    @property
    def spread_pct(self) -> Optional[float]:
        """Relative bid-ask spread — the liquidity filter for any tradeable signal."""
        m = self.mid
        if m is None or m <= 0:
            return None
        return (self.ask - self.bid) / m * 100.0


@dataclass(frozen=True)
class ChainSnapshot:
    """
    One symbol's full option chain for one expiry at one timestamp.

    `session_phase` matters: identical raw fields mean different things during
    CAS_WINDOW (cash book frozen) than during CONTINUOUS. Signals and backtests
    must be able to condition on it rather than inferring it from the clock.
    """
    ts:            str            # ISO8601 with IST offset — poll timestamp
    session_date:  str            # YYYY-MM-DD
    session_phase: SessionPhase
    symbol:        str
    expiry_date:   str            # DD-MM-YYYY, as Fyers reports it
    expiry_epoch:  int
    spot:          float
    futures:       Optional[float] = None   # populated once futures capture lands
    rows:          tuple[ChainRow, ...] = field(default_factory=tuple)

    # ── Convenience accessors used by signals ────────────────────────────────

    def strikes(self) -> list[float]:
        return sorted({r.strike for r in self.rows})

    def by_key(self) -> dict[tuple[float, str], ChainRow]:
        """(strike, option_type) → row, for O(1) lookup inside signal code."""
        return {(r.strike, r.option_type): r for r in self.rows}

    def atm_strike(self) -> Optional[float]:
        strikes = self.strikes()
        if not strikes:
            return None
        return min(strikes, key=lambda k: abs(k - self.spot))

    def total_oi(self, option_type: str) -> float:
        return sum(r.oi for r in self.rows if r.option_type == option_type)

    def pcr(self) -> Optional[float]:
        """Put-call ratio on total OI. None when there is no call OI to divide by."""
        call_oi = self.total_oi("CE")
        if call_oi <= 0:
            return None
        return self.total_oi("PE") / call_oi

    def to_dict(self) -> dict:
        d = asdict(self)
        d["session_phase"] = self.session_phase.value
        return d
