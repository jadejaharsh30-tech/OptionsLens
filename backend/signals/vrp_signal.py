# optionslens/backend/signals/vrp_signal.py
"""
Variance risk premium signal (roadmap item 30).

Hypothesis: when implied volatility is rich relative to what the underlying has
actually been realising, selling volatility pays, and when it is cheap, buying
it does. This is the best-documented premium in options and the one signal here
whose inputs were already built and already correct.

It is still a hypothesis. Nothing below asserts it works; the backtester says.

HOW HISTORY REACHES A PURE SIGNAL
    A signal sees `ChainSnapshot`s and nothing else, which is what makes live and
    backtest provably identical. But VRP ranks today's spread against two years
    of daily readings, far beyond any snapshot window — and recomputing IV from
    the snapshots would give FRONT-EXPIRY IV, reintroducing the weekly-roll
    artefact that `get_cm_iv_history` exists to remove.

    So the series arrives through `SignalContext.extras`, built by
    `vrp.load_vrp_history`, which the live endpoint and the backtest fixture both
    call. The signal never reads a database itself.

    Look-ahead is controlled at the point of use: every read goes through
    `vrp.observations_before(series, session_date)`, which is strictly `<`. The
    signal cannot see its own observation or any later one, and that is a tested
    property rather than a convention.
"""
from typing import Optional

from signals.base import Direction, Signal, SignalContext, SignalResult
from signals.registry import register_signal
from vrp import MIN_OBSERVATIONS, observations_before, vrp_percentile

SIGNAL_ID = "vrp"
VERSION = 1

EXTRAS_KEY = "vrp_series"


def _todays_row(series: list[dict], session_date: str) -> Optional[dict]:
    """The reading for this session, if the series carries one."""
    for row in reversed(series):
        if row.get("date") == session_date:
            return row
    return None


@register_signal(
    SIGNAL_ID,
    version=VERSION,
    description=("Variance risk premium: 30-day constant-maturity ATM IV minus "
                 "20-session realised vol, ranked as a percentile of its own history."),
    default_params={
        # Sell vol above this percentile, buy below the low one. Wide by design:
        # the tails are where the premium is claimed to live, and the backtester
        # can sweep these without the signal being re-run.
        "rich_percentile":  80.0,
        "cheap_percentile": 20.0,
        "min_observations": MIN_OBSERVATIONS,
        # Refuse to act on a spread this close to zero regardless of percentile:
        # in a flat regime the percentile can be extreme while the spread itself
        # is inside the noise of the IV measurement.
        "min_abs_vrp_points": 0.5,
    },
    min_history=0,
)
def vrp_signal(ctx: SignalContext) -> SignalResult:
    """
    Fires SHORT_VOL when the premium is historically rich, LONG_VOL when cheap.

    Direction is about volatility, not price: a rich premium says sell options,
    it says nothing about which way the underlying goes.
    """
    series = ctx.extras.get(EXTRAS_KEY) or []
    if not series:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            "no VRP series supplied — pass vrp.load_vrp_history() via extras")

    session_date = ctx.snapshot.session_date

    today = _todays_row(series, session_date)
    if today is None:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"no paired IV/RV reading for {session_date}")

    # Strictly prior observations only. Including today would rank a value
    # against a set containing itself.
    history = observations_before(series, session_date)
    min_obs = int(ctx.param("min_observations", MIN_OBSERVATIONS))

    current = today["vrp"]
    features = {
        "vrp_points":   current,
        "iv_pct":       round(today["iv"] * 100, 3),
        "rv_pct":       round(today["rv"] * 100, 3),
        "history_size": len(history),
        "session_date": session_date,
    }

    pct = vrp_percentile(history, current, min_obs)
    if pct is None:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"only {len(history)} prior observations, need {min_obs} before a "
            f"percentile means anything",
            features)

    features["vrp_percentile"] = round(pct, 2)

    rich  = ctx.param("rich_percentile", 80.0)
    cheap = ctx.param("cheap_percentile", 20.0)
    floor = ctx.param("min_abs_vrp_points", 0.5)

    if abs(current) < floor:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"spread {current:+.2f} vol points is inside the measurement noise "
            f"(floor {floor})", features)

    if pct >= rich:
        direction, label = Direction.SHORT_VOL, "rich"
    elif pct <= cheap:
        direction, label = Direction.LONG_VOL, "cheap"
    else:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"VRP at the {pct:.0f}th percentile, between {cheap} and {rich}",
            features)

    # Strength grows toward the tails, saturating at the extremes.
    strength = (pct - rich) / (100.0 - rich) if direction is Direction.SHORT_VOL \
        else (cheap - pct) / cheap
    strength = max(0.0, min(1.0, strength))

    return SignalResult.fire(Signal(
        signal_id = SIGNAL_ID,
        version   = VERSION,
        ts        = ctx.ts,
        symbol    = ctx.symbol,
        direction = direction,
        strength  = strength,
        features  = features,
        notes     = (f"VRP {current:+.2f} vol points "
                     f"(IV {today['iv'] * 100:.1f}% vs RV {today['rv'] * 100:.1f}%) "
                     f"at the {pct:.0f}th percentile of {len(history)} readings "
                     f"— historically {label}"),
    ))
