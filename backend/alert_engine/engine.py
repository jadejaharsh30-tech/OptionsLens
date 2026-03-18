# optionslens/backend/alert_engine/engine.py
"""
OI Institutional Alert Engine — ported from app_v2_final.py.

Changes from the original Streamlit app:
  - st.session_state  → engine_state singleton (models.py)
  - Single symbol     → outer loop over config.symbols
  - Hardcoded config  → EngineConfig dataclass fields
  - time.sleep        → asyncio.sleep
  - Streamlit UI      → pure Python returns (router handles responses)

Signal logic matches app_v2_final.py exactly:
  Stage 1 : oichp (Δ vs Settlement from Fyers) > threshold → enter pending_spikes
            Start-time independent — uses Fyers' own prev-day settlement calculation.
            When use_adaptive_threshold=True, threshold is the Nth percentile of
            historical oichp distribution for this symbol — makes Stage 1 a genuine
            outlier detector rather than an absolute cutoff.
  Stage 2 : oi_increasing vs SPIKE-POINT OI (not session open, not settlement)
            Correctly detects if writers still adding vs covering after spike
  Suppress: dual-side writing via oichp from chain_lookup (no DB queries)
            already-alerted-today guard
"""
import asyncio
import logging
from datetime import datetime, date

import pandas as pd

from alert_engine.db import (
    init_db, write_snapshots, write_baseline, get_baseline,
    get_recent_snapshots, write_alert, already_alerted_today,
)
from alert_engine.models import (
    EngineConfig, EngineState, PendingSpike, engine_state,
)
from alert_engine.percentile_threshold import get_adaptive_threshold
from alert_engine.models import ALERT_ENGINE_DB
from config import UNDERLYINGS
from fyers_client import get_fyers, fetch_quote, fetch_expiry_list, fetch_option_chain
from routers.chain import days_to_expiry

logger = logging.getLogger(__name__)


# ── Helper functions — direct ports from app_v2_final.py Section 5 ────────────

def round_to_strike(price: float, step: int) -> int:
    return int(round(price / step) * step)


def get_atm_strikes(cmp: float, step: int, n: int) -> list:
    atm = round_to_strike(cmp, step)
    return [atm + i * step for i in range(-n, n + 1)]


def compute_oi_pct_from_baseline(current_oi: float, baseline_oi: float) -> float:
    """
    DISPLAY ONLY — % change from session open baseline (shown as Δ vs Open in UI).
    NOT used for signal detection.
    Signal detection uses oichp (Δ vs Settlement) directly from Fyers row data.
    """
    if baseline_oi is None or baseline_oi == 0:
        return 0.0
    return ((current_oi - baseline_oi) / baseline_oi) * 100.0


def compute_oi_speed(df: pd.DataFrame) -> float:
    """
    OI velocity = % OI change within rolling window / elapsed minutes.
    Returns %/min. Higher = more aggressive positioning.
    """
    if len(df) < 2:
        return 0.0
    start_oi = df.iloc[0]['oi']
    end_oi   = df.iloc[-1]['oi']
    if start_oi == 0:
        return 0.0
    elapsed_minutes = max(
        (df.iloc[-1]['timestamp'] - df.iloc[0]['timestamp']).total_seconds() / 60.0,
        0.083   # floor at 5 seconds to avoid division explosion
    )
    return ((end_oi - start_oi) / start_oi) * 100.0 / elapsed_minutes


def classify_premium_behavior(oi_increasing: bool,
                               ltp_now: float,
                               ltp_baseline: float) -> str:
    """
    Ravi Bhatt directional matrix — unchanged from app_v2_final.py.
    OI direction × premium direction → buildup classification.
    """
    premium_up = ltp_now > ltp_baseline
    if oi_increasing and not premium_up:
        return "SHORT_BUILDUP"      # OI↑ Premium↓ → writers selling
    elif oi_increasing and premium_up:
        return "LONG_BUILDUP"       # OI↑ Premium↑ → buyers coming in
    elif not oi_increasing and premium_up:
        return "SHORT_COVERING"
    else:
        return "LONG_UNWINDING"


def map_signal_to_trade(option_type: str, premium_behavior: str):
    """
    Only SHORT_BUILDUP generates a trade signal.
      Put SHORT_BUILDUP  → BUY CALL  (institutions writing puts = bullish)
      Call SHORT_BUILDUP → BUY PUT   (institutions writing calls = bearish)
    Returns (signal_direction, trade_option) or (None, None).
    """
    if premium_behavior != "SHORT_BUILDUP":
        return None, None
    if option_type == "PE":
        return "BULLISH", "CE"
    elif option_type == "CE":
        return "BEARISH", "PE"
    return None, None


def is_dual_side_writing(strike: float, chain_lookup: dict) -> bool:
    """
    Suppression filter: if BOTH CE and PE at this strike have oichp > 200%
    simultaneously, it's event hedging (straddle/strangle) — suppress alert.
    """
    ce_row = chain_lookup.get((strike, "CE"))
    pe_row = chain_lookup.get((strike, "PE"))
    if ce_row is None or pe_row is None:
        return False
    ce_pct = ce_row.get('oi_change_pct', 0)
    pe_pct = pe_row.get('oi_change_pct', 0)
    return ce_pct >= 200 and pe_pct >= 200


def score_confidence(oi_pct: float, oi_speed: float, volume: float) -> str:
    """3-tier confidence scoring — unchanged from app_v2_final.py."""
    score = 0
    if oi_pct >= 700:    score += 3
    elif oi_pct >= 500:  score += 2
    if oi_speed >= 50:   score += 2
    elif oi_speed >= 20: score += 1
    if volume >= 1000:   score += 1
    if score >= 5:   return "HIGH"
    elif score >= 3: return "MEDIUM"
    return "LOW"


def is_market_open() -> bool:
    """True between 09:15 and 15:30 IST on weekdays."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


# ── Per-symbol engine tick ─────────────────────────────────────────────────────

def run_symbol_tick(fyers, symbol: str, session_date: str,
                    cfg: EngineConfig, state: EngineState) -> list:
    """
    One full engine cycle for one symbol.
    Direct port of run_engine_tick() from app_v2_final.py,
    extended with symbol parameter and adaptive threshold support.
    Returns list of new confirmed alert dicts generated this tick.
    """
    new_alerts = []
    now_str    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sym_cfg    = UNDERLYINGS[symbol]

    # ── Fetch spot + expiry ───────────────────────────────────────────────────
    spot     = fetch_quote(fyers, symbol)
    expiries = fetch_expiry_list(fyers, symbol)
    if not expiries:
        logger.warning(f"[{symbol}] No expiries returned — skipping tick.")
        return []

    exp = next((e for e in expiries if days_to_expiry(e["date"]) > 0), None)
    if exp is None:
        logger.warning(f"[{symbol}] All expiries past — skipping tick.")
        return []

    expiry_date  = exp["date"]
    expiry_epoch = exp["expiry"]

    # ── Determine ATM strike range ────────────────────────────────────────────
    atm_strikes = get_atm_strikes(spot, sym_cfg["strike_step"], cfg.strikes_either_side)

    # ── Fetch option chain ────────────────────────────────────────────────────
    chain = fetch_option_chain(fyers, symbol, expiry_epoch,
                               strike_count=cfg.strikes_either_side * 2 + 2)
    if not chain:
        return []

    # Build lookup: (strike, opt_type) → row dict (for dual-side check + snapshot)
    chain_lookup = {(r["strike"], r["option_type"]): r for r in chain}

    # ── Write OI snapshots to DB ──────────────────────────────────────────────
    snapshot_rows = []
    for r in chain:
        if r["strike"] not in atm_strikes:
            continue
        baseline = get_baseline(session_date, symbol, r["strike"], r["option_type"])
        baseline_oi = baseline[0] if baseline else None
        oi_pct_from_open = compute_oi_pct_from_baseline(r["oi"], baseline_oi)
        snapshot_rows.append({
            "timestamp":      now_str,
            "session_date":   session_date,
            "symbol":         symbol,
            "expiry_date":    expiry_date,
            "strike":         r["strike"],
            "option_type":    r["option_type"],
            "oi":             r["oi"],
            "oi_change":      r.get("oi_change", 0),
            "oi_change_pct":  r.get("oi_change_pct", 0),
            "prev_oi":        r.get("prev_oi", 0),
            "ltp":            r["ltp"],
            "volume":         r.get("volume", 0),
        })
    if snapshot_rows:
        write_snapshots(snapshot_rows)

    # Update live snapshot for UI display
    state.chain_snapshot[symbol] = snapshot_rows

    # ── Per-strike signal detection ───────────────────────────────────────────
    pending = state.pending_spikes.setdefault(symbol, {})

    for r in chain:
        strike     = r["strike"]
        opt_type   = r["option_type"]
        key        = (strike, opt_type)

        if strike not in atm_strikes:
            continue

        current_oi  = r["oi"]
        current_ltp = r["ltp"]
        current_vol = r.get("volume", 0)

        # Volume gate
        if current_vol < cfg.min_volume_filter:
            continue

        # oichp — Δ vs Settlement (Fyers calculates this directly)
        oi_pct = r.get("oi_change_pct", 0)

        # OI speed
        recent_df = get_recent_snapshots(symbol, strike, opt_type, cfg.oi_speed_window_min)
        oi_speed  = compute_oi_speed(recent_df)

        # Session baseline — written for display only, NOT for signal detection
        baseline = get_baseline(session_date, symbol, strike, opt_type)
        if baseline is None:
            write_baseline(session_date, symbol, strike, opt_type,
                           current_oi, current_ltp, now_str)

        # ── STAGE 1: Spike detection ──────────────────────────────────────────
        # Resolve threshold: adaptive (percentile-based) or static flat value
        if cfg.use_adaptive_threshold:
            threshold = get_adaptive_threshold(
                db_path=ALERT_ENGINE_DB,
                symbol=symbol,
                option_type=opt_type,
                percentile=cfg.adaptive_percentile,
                min_samples=50,
                static_fallback=cfg.oi_spike_threshold_pct,
            )
        else:
            threshold = cfg.oi_spike_threshold_pct

        if (oi_pct >= threshold
                and key not in pending
                and not already_alerted_today(session_date, symbol, strike, opt_type)):
            pending[key] = PendingSpike(
                fired_at=datetime.now(),
                ltp_at_spike=current_ltp,
                oi_at_spike=current_oi,
                oi_pct=oi_pct,
                oi_speed=oi_speed,
            )
            logger.info(
                f"[{symbol}] Stage 1 fired: {strike}{opt_type} "
                f"oichp +{oi_pct:.0f}% vs settlement "
                f"(threshold: {threshold:.0f}%"
                f"{' adaptive' if cfg.use_adaptive_threshold else ' static'})"
            )

        # ── STAGE 2: Premium confirmation window ──────────────────────────────
        if key in pending:
            spike = pending[key]
            spike.polls_waited += 1

            prem_behavior = classify_premium_behavior(
                oi_increasing=(current_oi > spike.oi_at_spike),
                ltp_now=current_ltp,
                ltp_baseline=spike.ltp_at_spike,
            )

            signal_dir, trade_opt = map_signal_to_trade(opt_type, prem_behavior)
            confirmed  = signal_dir is not None
            timed_out  = spike.polls_waited >= cfg.premium_confirm_polls

            if not (confirmed or timed_out):
                continue

            # Remove from pending regardless of outcome
            del pending[key]

            base_record = {
                "triggered_at":    now_str,
                "session_date":    session_date,
                "symbol":          symbol,
                "expiry_date":     expiry_date,
                "strike":          strike,
                "option_type":     opt_type,
                "trade_strike":    strike,
                "trade_option":    trade_opt,
                "oi_pct_change":   round(spike.oi_pct, 2),
                "oi_speed_pct_pm": round(spike.oi_speed, 2),
                "ltp_at_trigger":  spike.ltp_at_spike,
                "ltp_confirmed":   current_ltp,
                "premium_behavior": prem_behavior,
            }

            if not confirmed:
                write_alert({**base_record,
                    "signal_direction": None,
                    "confidence":       "N/A",
                    "suppressed":       1,
                    "suppress_reason":  "PREMIUM_NOT_CONFIRMED"})
                continue

            if is_dual_side_writing(strike, chain_lookup):
                write_alert({**base_record,
                    "signal_direction": signal_dir,
                    "confidence":       "SUPPRESSED",
                    "suppressed":       1,
                    "suppress_reason":  "DUAL_SIDE_WRITING"})
                continue

            # ── CONFIRMED ALERT ───────────────────────────────────────────────
            confidence = score_confidence(spike.oi_pct, spike.oi_speed, current_vol)
            alert = {**base_record,
                     "signal_direction": signal_dir,
                     "confidence":       confidence,
                     "suppressed":       0,
                     "suppress_reason":  None}

            write_alert(alert)
            new_alerts.append(alert)
            state.alert_count_session += 1

            logger.info(
                f"CONFIRMED ALERT [{confidence}] {symbol} "
                f"{strike}{opt_type} → {signal_dir} | "
                f"OI: +{spike.oi_pct:.0f}% | Speed: {spike.oi_speed:.1f}%/min"
            )

    return new_alerts


# ── Async task runner ─────────────────────────────────────────────────────────

async def engine_task(token: str, cfg: EngineConfig):
    """
    Main async loop. Runs until engine_state.running is set to False.
    One iteration = poll all configured symbols sequentially.
    asyncio.sleep() yields control so FastAPI stays responsive during waits.
    """
    init_db()
    fyers = get_fyers(token)

    engine_state.running             = True
    engine_state.config              = cfg
    engine_state.started_at          = datetime.now()
    engine_state.last_poll_status    = "starting"
    engine_state.alert_count_session = 0
    engine_state.pending_spikes      = {}
    engine_state.chain_snapshot      = {}

    logger.info(
        f"Engine started — symbols: {cfg.symbols} | "
        f"interval: {cfg.poll_interval_sec}s | "
        f"spike threshold: {cfg.oi_spike_threshold_pct}%"
        f"{' (adaptive ' + str(cfg.adaptive_percentile) + 'th pct)' if cfg.use_adaptive_threshold else ''}"
    )

    while engine_state.running:
        try:
            if not is_market_open():
                engine_state.last_poll_status = "market_closed"
                engine_state.last_poll_at     = datetime.now()
                await asyncio.sleep(cfg.poll_interval_sec)
                continue

            session_date = date.today().isoformat()

            for symbol in cfg.symbols:
                if not engine_state.running:
                    break
                try:
                    run_symbol_tick(fyers, symbol, session_date, cfg, engine_state)
                except Exception as sym_err:
                    logger.error(f"Tick error [{symbol}]: {repr(sym_err)}")

            engine_state.last_poll_at     = datetime.now()
            engine_state.last_poll_status = "ok"
            engine_state.last_error       = None

        except asyncio.CancelledError:
            logger.info("Engine task cancelled.")
            break
        except Exception as e:
            engine_state.last_poll_status = "error"
            engine_state.last_error       = repr(e)
            logger.error(f"Engine loop error: {repr(e)}")

        await asyncio.sleep(cfg.poll_interval_sec)

    engine_state.running = False
    logger.info("Engine stopped.")
