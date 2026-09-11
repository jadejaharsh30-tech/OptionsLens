# optionslens/backend/alert_engine/models.py
"""
Dataclasses for engine configuration and runtime state.
Shared between engine.py and the FastAPI router.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import ALERT_ENGINE_DB  # noqa: F401  (re-exported; env-configurable)


@dataclass
class EngineConfig:
    """
    User-configurable thresholds. Set from the UI before starting.
    Defaults match the proven values from app_v2_final.py.
    """
    symbols:                list  = field(default_factory=lambda: ["NIFTY", "BANKNIFTY"])
    poll_interval_sec:      int   = 10
    oi_spike_threshold_pct: float = 500.0
    oi_speed_window_min:    int   = 5
    premium_confirm_polls:  int   = 4
    min_volume_filter:      int   = 100
    strikes_either_side:    int   = 1
    # ── Adaptive threshold fields ─────────────────────────────────────────────
    use_adaptive_threshold: bool  = False   # if True, derive threshold from history
    adaptive_percentile:    float = 90.0    # Nth percentile of historical oichp values


@dataclass
class PendingSpike:
    """One entry in the Stage 1 → Stage 2 confirmation window."""
    fired_at:     datetime
    ltp_at_spike: float
    oi_at_spike:  float    # OI at moment Stage 1 fired — used for oi_increasing in Stage 2
    oi_pct:       float
    oi_speed:     float
    polls_waited: int = 0


@dataclass
class EngineState:
    """
    Shared mutable state between the async engine task and the FastAPI router.
    Router reads this; engine writes it. Single-threaded asyncio — no locks needed.
    """
    running:             bool  = False
    config:              Optional[EngineConfig] = None
    started_at:          Optional[datetime]     = None
    last_poll_at:        Optional[datetime]     = None
    last_poll_status:    str   = "idle"   # "ok" | "error" | "market_closed" | "idle"
    last_error:          Optional[str]    = None
    alert_count_session: int   = 0
    # { symbol: { (strike, opt_type): PendingSpike } }
    pending_spikes:      dict  = field(default_factory=dict)
    # Live chain snapshot per symbol for UI display table
    chain_snapshot:      dict  = field(default_factory=dict)


# Module-level singleton — one instance for the entire FastAPI process
engine_state = EngineState()
