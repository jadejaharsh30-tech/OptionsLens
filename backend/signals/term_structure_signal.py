# optionslens/backend/signals/term_structure_signal.py
"""
Term-structure signal (roadmap item 33, first half).

Hypothesis: the SHAPE of the volatility curve carries information the level does
not. When the curve is steeply upward-sloping, front-month implied is cheap
relative to the back and rolls DOWN toward spot as it ages, so being short front
vol earns the roll. When the curve inverts, the front rolls UP, and inversion is
also the market's own statement that it expects more volatility in the next
month than in the next two — historically a realised-vol expansion, not a
mispricing to fade.

    steep contango  (high slope percentile)  -> SHORT_VOL
    inversion       (low slope percentile)   -> LONG_VOL

That is the VIX-futures carry argument transplanted to options, and it is a
hypothesis, not a finding. The backtester decides.

TWO THINGS TO KNOW BEFORE READING A RESULT FROM THIS

1.  **It is not independent of `vrp`.** Both are driven by the same calm/stress
    regime: calm means low realised vol, a positive premium AND a steep curve;
    stress means realised above implied AND an inverted curve. So the two
    signals will agree most of the time. An ensemble (item 36) that counts them
    as two confirmations is counting the regime twice.

2.  **Firing is regime-conditional, so the null is doing real work.** This
    signal mostly fires in calm markets, and calm markets have a positive
    variance risk premium on average. A short-vol rule that only trades calm
    days can therefore show a positive vol outcome while adding nothing over
    "sell vol when it is quiet". The weekday-matched null spans both regimes,
    which is what separates the two; a headline hit rate without it does not.

HOW THE CURVE REACHES A PURE SIGNAL
    A signal sees `ChainSnapshot`s only, and a snapshot holds ONE expiry — the
    curve is not in it by construction. Nor could it be rebuilt from the replay
    window: the EOD adapter materialises the nearest expiry per session, so
    every bar in history is the same leg.

    So the slope series arrives through `SignalContext.extras`, built by
    `term_structure.load_term_structure_history`, exactly as `vrp` receives its
    spread. The signal reads no database.

    Look-ahead is controlled at the point of use by `vrp.observations_before`,
    which is strictly `<` and is the single tested guard every ranked signal
    goes through.
"""
from typing import Optional

from signals.base import Direction, Signal, SignalContext, SignalResult
from signals.registry import register_signal
from term_structure import MIN_OBSERVATIONS, slope_percentile
# The one look-ahead guard, deliberately shared rather than re-derived: `<`
# versus `<=` is a one-character error that leaks today's reading into its own
# percentile and is invisible in the output.
from vrp import observations_before

SIGNAL_ID = "term_structure"
VERSION = 1

EXTRAS_KEY = "term_structure_series"


def _todays_row(series: list[dict], session_date: str) -> Optional[dict]:
    """The curve reading for this session, if the series carries one."""
    for row in reversed(series):
        if row.get("date") == session_date:
            return row
    return None


@register_signal(
    SIGNAL_ID,
    version=VERSION,
    description=("Volatility term structure: 60-day minus 30-day constant-maturity "
                 "ATM IV, ranked as a percentile of its own history. Steep "
                 "contango sells vol, inversion buys it."),
    default_params={
        "steep_percentile":    80.0,
        "inverted_percentile": 20.0,
        "min_observations":    MIN_OBSERVATIONS,
        # Percentile alone is not enough in either tail, and the two guards are
        # not symmetric because the curve is not.
        #
        # An index curve is in contango almost all the time, so its 20th
        # percentile can still be a healthy +1.2 vol points. Calling that
        # "inversion" and buying vol on it would be trading a mildly-less-steep
        # normal curve as though it were stress. LONG_VOL therefore requires the
        # slope to be genuinely at or below `max_slope_for_long`.
        "max_slope_for_long": 0.0,
        # And the steep tail needs the curve to actually slope: in a flat regime
        # the 80th percentile can sit a few hundredths of a point above zero,
        # inside the noise of two interpolated IV readings.
        "min_slope_for_short": 0.25,
    },
    min_history=0,
)
def term_structure_signal(ctx: SignalContext) -> SignalResult:
    """
    Fires SHORT_VOL on a historically steep curve, LONG_VOL on a genuine inversion.

    Direction is about volatility, not price: an inverted curve says buy
    volatility, it says nothing about which way the underlying goes.
    """
    series = ctx.extras.get(EXTRAS_KEY) or []
    if not series:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            "no term-structure series supplied — pass "
            "term_structure.load_term_structure_history() via extras")

    session_date = ctx.snapshot.session_date

    today = _todays_row(series, session_date)
    if today is None:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"no paired near/far constant-maturity IV for {session_date} — the "
            f"far leg needs a traded expiry beyond the far tenor")

    history = observations_before(series, session_date)
    min_obs = int(ctx.param("min_observations", MIN_OBSERVATIONS))

    current = today["slope"]
    features = {
        "slope_points": current,
        "near_iv_pct":  round(today["near_iv"] * 100, 3),
        "far_iv_pct":   round(today["far_iv"] * 100, 3),
        "inverted":     current < 0,
        "history_size": len(history),
        "session_date": session_date,
    }

    pct = slope_percentile(history, current, min_obs)
    if pct is None:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"only {len(history)} prior curve observations, need {min_obs} "
            f"before a percentile means anything",
            features)

    features["slope_percentile"] = round(pct, 2)

    steep    = ctx.param("steep_percentile", 80.0)
    inverted = ctx.param("inverted_percentile", 20.0)

    if pct >= steep:
        floor = ctx.param("min_slope_for_short", 0.25)
        if current < floor:
            return SignalResult.skip(
                SIGNAL_ID, VERSION, ctx,
                f"{pct:.0f}th percentile but the curve is only {current:+.2f} "
                f"vol points steep — a flat curve's top decile is still flat",
                features)
        direction, label = Direction.SHORT_VOL, "steep"
        strength = (pct - steep) / (100.0 - steep)

    elif pct <= inverted:
        ceiling = ctx.param("max_slope_for_long", 0.0)
        if current > ceiling:
            return SignalResult.skip(
                SIGNAL_ID, VERSION, ctx,
                f"{pct:.0f}th percentile but the curve is still in contango at "
                f"{current:+.2f} vol points — a flatter normal curve is not stress",
                features)
        direction, label = Direction.LONG_VOL, "inverted"
        strength = (inverted - pct) / inverted if inverted else 1.0

    else:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"curve at the {pct:.0f}th percentile, between {inverted} and {steep}",
            features)

    return SignalResult.fire(Signal(
        signal_id = SIGNAL_ID,
        version   = VERSION,
        ts        = ctx.ts,
        symbol    = ctx.symbol,
        direction = direction,
        strength  = max(0.0, min(1.0, strength)),
        features  = features,
        notes     = (f"term structure {current:+.2f} vol points "
                     f"({today['far_iv'] * 100:.1f}% far vs "
                     f"{today['near_iv'] * 100:.1f}% near) at the "
                     f"{pct:.0f}th percentile of {len(history)} readings "
                     f"— historically {label}"),
    ))
