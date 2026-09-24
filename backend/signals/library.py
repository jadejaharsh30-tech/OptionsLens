# optionslens/backend/signals/library.py
"""
Registered signals.

Importing this module registers everything in it, so it must be imported once
at startup (main.py does this) before the registry is queried.

Each signal here is a hypothesis, not a conviction. None of them is known to
work on this data yet — that is what the backtester is for, and until a signal
has beaten the matched null out of sample it should be treated as a guess with
good motivation.
"""
from signals.base import Direction, Signal, SignalContext, SignalResult
from signals.features import compute_features, gex_by_strike
from signals.registry import register_signal

# Registers the ported alert-engine rule (roadmap item 37). Imported for its
# side effect; the signal itself lives in its own module because it is large.
import signals.oi_buildup  # noqa: F401,E402


@register_signal(
    "gex_regime",
    version=1,
    description="Dealer gamma regime from the zero-gamma flip level.",
    default_params={
        # How far spot must sit from the flip level, as a % of spot, before the
        # regime is considered established rather than noise around the cross.
        "flip_buffer_pct": 0.15,
        # Minimum |net GEX| for the profile to be meaningful at all.
        "min_abs_gex": 0.0,
        "min_strikes": 6,
    },
    min_history=0,
)
def gex_regime(ctx: SignalContext) -> SignalResult:
    """
    Trade the dealer-hedging regime rather than the raw GEX number.

    Mechanism: if dealers are net long gamma, hedging means selling into rallies
    and buying dips, which dampens realised vol and pins price toward high-gamma
    strikes. Net short gamma inverts it — hedging chases the move and amplifies
    it. The zero-gamma flip level is where that behaviour changes sign, so it is
    the tradeable quantity; the absolute GEX figure mostly reflects open
    interest levels and is not comparable across days.

    Above the flip  -> dealers long gamma  -> mean reversion, SHORT_VOL
    Below the flip  -> dealers short gamma -> trend continuation, LONG_VOL
    """
    spec_id, version = "gex_regime", 1
    feats = compute_features(ctx.snapshot)

    if feats.forward is None:
        return SignalResult.skip(spec_id, version, ctx,
                                 "no implied forward — chain not priceable")

    profile = gex_by_strike(feats)
    if len(profile) < ctx.param("min_strikes", 6):
        return SignalResult.skip(spec_id, version, ctx,
                                 f"only {len(profile)} strikes with gamma")

    if feats.gamma_flip is None:
        return SignalResult.skip(spec_id, version, ctx,
                                 "net GEX does not cross zero in this strike range",
                                 {"net_gex": feats.net_gex})

    if feats.net_gex is None or abs(feats.net_gex) <= ctx.param("min_abs_gex", 0.0):
        return SignalResult.skip(spec_id, version, ctx, "net GEX below floor",
                                 {"net_gex": feats.net_gex})

    spot     = feats.spot
    distance = (spot - feats.gamma_flip) / spot * 100.0
    buffer_  = ctx.param("flip_buffer_pct", 0.15)

    features = {
        "net_gex":        feats.net_gex,
        "gamma_flip":     round(feats.gamma_flip, 2),
        "spot":           spot,
        "distance_pct":   round(distance, 4),
        "atm_iv":         feats.atm_iv,
        "pcr_oi":         round(feats.pcr_oi, 4),
        "max_pain":       feats.max_pain,
        "rr_25d":         feats.rr_25d,
        "session_phase":  ctx.snapshot.session_phase.value,
    }

    if abs(distance) < buffer_:
        return SignalResult.skip(
            spec_id, version, ctx,
            f"spot within {buffer_}% of flip ({distance:+.3f}%) — regime unclear",
            features,
        )

    # Strength scales with distance from the flip, saturating at 1% away. The
    # threshold is a parameter; keeping the continuous score lets the backtester
    # sweep it after the fact instead of re-running every evaluation.
    strength = min(1.0, abs(distance) / 1.0)

    return SignalResult.fire(Signal(
        signal_id = spec_id,
        version   = version,
        ts        = ctx.ts,
        symbol    = ctx.symbol,
        direction = Direction.SHORT_VOL if distance > 0 else Direction.LONG_VOL,
        strength  = strength,
        features  = features,
        notes     = (
            f"spot {distance:+.3f}% "
            f"{'above' if distance > 0 else 'below'} gamma flip "
            f"{feats.gamma_flip:.0f} — dealers "
            f"{'long' if distance > 0 else 'short'} gamma"
        ),
    ))
