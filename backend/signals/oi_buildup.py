# optionslens/backend/signals/oi_buildup.py
"""
The alert engine's SHORT_BUILDUP rule, ported into the signal framework so it
can be measured instead of argued about (roadmap item 37).

WHY THIS EXISTS
---------------
The live rule fired three BEARISH / BUY-PE alerts on NIFTY 23050-23150 CE at
14:39-14:40 on 2026-09-24, during an up-trending session. Its logic is "call OI
up + call premium down = call writing = bearish", but four days before a weekly
expiry the premium falls from theta alone, so the rule may simply be detecting
time decay and labelling it positioning. That is a hypothesis about a bug, and
the only way to settle it is a backtest against the matched null.

THE PORT
--------
The alert engine carries `pending_spikes` as mutable state across polls: stage 1
records a spike, stage 2 re-checks it on later polls. A signal function cannot
do that — it is pure over (snapshot, history), which is exactly what makes it
replayable. So the pending state is RECONSTRUCTED from the history window: for
each contract we look back up to `premium_confirm_polls` bars for the most
recent bar whose oichp crossed the threshold, and compare the current bar
against that one.

This is behaviourally equivalent for a signal evaluated on every bar, and it is
stateless, so the backtester and the live runner cannot diverge.

DELIBERATE DIFFERENCES from the live engine, each one a thing that cannot be
expressed in a pure function and is not needed to measure the rule:
  - no `already_alerted_today` guard. That is alert hygiene, not signal logic,
    and suppressing repeats would corrupt the denominator the backtest needs.
  - no adaptive percentile threshold. It reads a separate database; the static
    threshold is a parameter the backtester can sweep instead.
  - no session-open baseline. The engine itself documents it as display-only.
"""
from typing import Optional

from recorder.models import ChainRow, ChainSnapshot
from signals.base import Direction, Signal, SignalContext, SignalResult
from signals.registry import register_signal

SIGNAL_ID = "oi_short_buildup"
VERSION = 1


# ── Pure re-implementations of the engine's classification ───────────────────
# Deliberately re-implemented rather than imported: `alert_engine.engine` pulls
# in fyers_client and its own database at module scope, and a signal that can
# reach a broker client breaks the guarantee that live and backtest are the same
# code. `test_oi_buildup.py` asserts these agree with the engine's versions
# across all four quadrants, so the fidelity claim is checked, not asserted.

def classify_premium_behavior(oi_increasing: bool, ltp_now: float,
                              ltp_baseline: float) -> str:
    """OI direction x premium direction -> buildup label."""
    premium_up = ltp_now > ltp_baseline
    if oi_increasing and not premium_up:
        return "SHORT_BUILDUP"
    if oi_increasing and premium_up:
        return "LONG_BUILDUP"
    if not oi_increasing and premium_up:
        return "SHORT_COVERING"
    return "LONG_UNWINDING"


def map_signal_to_trade(option_type: str, premium_behavior: str):
    """
    Only SHORT_BUILDUP trades. Writing puts reads as bullish, writing calls as
    bearish — the inference this port exists to test.
    """
    if premium_behavior != "SHORT_BUILDUP":
        return None, None
    if option_type == "PE":
        return "BULLISH", "CE"
    if option_type == "CE":
        return "BEARISH", "PE"
    return None, None


def score_confidence(oi_pct: float, oi_speed: float, volume: float) -> str:
    score = 0
    if oi_pct >= 700:
        score += 3
    elif oi_pct >= 500:
        score += 2
    if oi_speed >= 50:
        score += 2
    elif oi_speed >= 20:
        score += 1
    if volume >= 1000:
        score += 1
    if score >= 5:
        return "HIGH"
    if score >= 3:
        return "MEDIUM"
    return "LOW"


_CONFIDENCE_STRENGTH = {"LOW": 0.33, "MEDIUM": 0.66, "HIGH": 1.0}


# ── Helpers over snapshots ───────────────────────────────────────────────────

def atm_band(snapshot: ChainSnapshot, n_either_side: int) -> set[float]:
    """
    The strikes either side of the money, taken from the chain's own strike
    ladder rather than `config.strike_step`.

    The config step is unverified for the stocks after bonus issues (an open
    question in the roadmap), and the ladder is the ground truth anyway.
    """
    strikes = snapshot.strikes()
    if not strikes:
        return set()
    atm = min(strikes, key=lambda k: abs(k - snapshot.spot))
    i = strikes.index(atm)
    lo = max(0, i - n_either_side)
    return set(strikes[lo:i + n_either_side + 1])


def find_spike_bar(history: tuple[ChainSnapshot, ...], strike: float,
                   option_type: str, threshold: float) -> Optional[ChainRow]:
    """
    The most recent bar in `history` where this contract's oichp crossed the
    threshold — the alert engine's stage 1, recovered from the window.

    Searched newest-first so a fresh spike supersedes a stale one, matching the
    engine's behaviour of overwriting a pending entry it has already cleared.
    """
    for snap in reversed(history):
        row = snap.by_key().get((strike, option_type))
        if row is not None and row.oi_change_pct >= threshold:
            return row
    return None


def oi_speed_pct_per_min(history: tuple[ChainSnapshot, ...],
                         current: ChainRow, strike: float, option_type: str,
                         interval_sec: float = 60.0) -> float:
    """
    OI velocity in %/min across the window, matching `compute_oi_speed`.

    Bars are assumed evenly spaced, which the recorder guarantees by aligning
    polls to interval boundaries.
    """
    if not history:
        return 0.0
    first = None
    for snap in history:
        row = snap.by_key().get((strike, option_type))
        if row is not None:
            first = row
            break
    if first is None or first.oi == 0:
        return 0.0
    elapsed_min = max(len(history) * interval_sec / 60.0, 0.083)
    return (current.oi - first.oi) / first.oi * 100.0 / elapsed_min


def is_dual_side_writing(snapshot: ChainSnapshot, strike: float,
                         threshold: float) -> bool:
    """Both legs spiking at one strike reads as event hedging, not direction."""
    keys = snapshot.by_key()
    ce = keys.get((strike, "CE"))
    pe = keys.get((strike, "PE"))
    if ce is None or pe is None:
        return False
    return ce.oi_change_pct >= threshold and pe.oi_change_pct >= threshold


# ── The signal ───────────────────────────────────────────────────────────────

@register_signal(
    SIGNAL_ID,
    version=VERSION,
    description=("Alert engine's OI SHORT_BUILDUP rule, ported for measurement. "
                 "Unvalidated: suspected of reading theta decay as call writing."),
    default_params={
        "oi_spike_threshold_pct": 500.0,
        "premium_confirm_polls":  4,
        "min_volume":             100.0,
        "strikes_either_side":    1,
        "suppress_dual_side":     True,
        "dual_side_threshold":    200.0,
        "interval_sec":           60.0,
    },
    min_history=1,
)
def oi_short_buildup(ctx: SignalContext) -> SignalResult:
    """
    Fires when an ATM-band contract spiked on OI versus prior settlement and its
    premium has since fallen while OI kept rising.

    Direction is the engine's inference: a call written is bearish, a put
    written is bullish.
    """
    snap = ctx.snapshot
    threshold   = ctx.param("oi_spike_threshold_pct", 500.0)
    window      = int(ctx.param("premium_confirm_polls", 4))
    min_volume  = ctx.param("min_volume", 100.0)
    n_strikes   = int(ctx.param("strikes_either_side", 1))
    interval    = ctx.param("interval_sec", 60.0)

    band = atm_band(snap, n_strikes)
    if not band:
        return SignalResult.skip(SIGNAL_ID, VERSION, ctx, "chain has no strikes")

    history = ctx.lookback(window)
    if not history:
        return SignalResult.skip(SIGNAL_ID, VERSION, ctx, "no history in window")

    keys = snap.by_key()
    candidates: list[tuple[float, dict]] = []
    examined = 0
    thin = 0

    for (strike, option_type), row in keys.items():
        if strike not in band:
            continue
        examined += 1

        if row.volume < min_volume:
            thin += 1
            continue

        spike = find_spike_bar(history, strike, option_type, threshold)
        if spike is None:
            continue

        behavior = classify_premium_behavior(
            oi_increasing = row.oi > spike.oi,
            ltp_now       = row.ltp,
            ltp_baseline  = spike.ltp,
        )
        direction, trade_option = map_signal_to_trade(option_type, behavior)
        if direction is None:
            continue

        if (ctx.param("suppress_dual_side", True)
                and is_dual_side_writing(snap, strike,
                                         ctx.param("dual_side_threshold", 200.0))):
            continue

        speed = oi_speed_pct_per_min(history, row, strike, option_type, interval)
        confidence = score_confidence(spike.oi_change_pct, speed, row.volume)

        candidates.append((spike.oi_change_pct, {
            "strike":            strike,
            "option_type":       option_type,
            "trade_option":      trade_option,
            "direction":         direction,
            "premium_behavior":  behavior,
            "oi_pct_change":     round(spike.oi_change_pct, 2),
            "oi_speed_pct_pm":   round(speed, 2),
            "ltp_at_spike":      spike.ltp,
            "ltp_now":           row.ltp,
            "oi_at_spike":       spike.oi,
            "oi_now":            row.oi,
            "volume":            row.volume,
            "confidence":        confidence,
            "session_phase":     snap.session_phase.value,
            "spot":              snap.spot,
        }))

    if not candidates:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"no confirmed buildup in {examined} band contracts "
            f"({thin} below volume floor)",
            {"examined": examined, "below_volume": thin, "threshold": threshold},
        )

    # Strongest spike wins when several confirm on the same bar.
    _, best = max(candidates, key=lambda c: c[0])

    return SignalResult.fire(Signal(
        signal_id = SIGNAL_ID,
        version   = VERSION,
        ts        = ctx.ts,
        symbol    = ctx.symbol,
        direction = Direction.BULLISH if best["direction"] == "BULLISH" else Direction.BEARISH,
        strength  = _CONFIDENCE_STRENGTH[best["confidence"]],
        features  = {**best, "candidates": len(candidates)},
        notes     = (f"{best['option_type']} {best['strike']:.0f} SHORT_BUILDUP "
                     f"(+{best['oi_pct_change']:.0f}% OI vs settle, premium "
                     f"{best['ltp_at_spike']:.2f} -> {best['ltp_now']:.2f}) "
                     f"-> {best['direction']}, buy {best['trade_option']}"),
    ))
