# optionslens/backend/signals/skew_signal.py
"""
25-delta risk-reversal signal (roadmap item 33, second half).

The risk reversal is a positioning gauge: how much more the market is paying for
downside protection than for upside participation. It is persistently negative
on an index, so only its percentile against its own history says anything.

THE DIRECTION IS GENUINELY CONTESTED, AND THAT IS WHY IT IS A PARAMETER
    Two readings of the same observation, both defensible:

    contrarian — an unusually bid put wing is hedging demand at its peak. Crash
        insurance is being bought after the fear, not before it, and the wing is
        richest near the point where the selling exhausts. Fade it: BULLISH.

    momentum — an unusually bid put wing is informed positioning. Someone is
        paying up for downside ahead of a repricing, and skew steepening leads
        drawdowns rather than marking their end. Follow it: BEARISH.

    The published evidence on index skew as a return predictor is mixed, and I
    am not going to pretend a docstring settles it. `mode` selects which
    hypothesis is under test and is recorded in the run's parameters, so a
    result is always attributable to the version that produced it.

    A WARNING THAT COMES WITH THAT CONVENIENCE: running both modes over the same
    sample and keeping the better one is two tests reported as one. The
    matched-null verdict does not know you ran it twice. The honest procedure is
    to fix the mode in advance, and if the other reading is worth testing, test
    it on data the first one did not see. `backtest.walkforward` exists for
    exactly this.

WHERE THE NUMBERS COME FROM
    Today's reading is derived from the snapshot the signal is holding — it is
    in the bar, so it stays pure. The HISTORY it is ranked against arrives
    through `extras`, built in one pass by `skew.load_skew_history`, because
    re-deriving a risk reversal across the whole replay window on every bar is
    the same chain-wide IV solve repeated once per bar.

    Both paths go through `skew.rr25_from_snapshot`, so the current reading and
    the historical ones are the same quantity. Look-ahead is controlled by
    `vrp.observations_before`, strictly `<`.
"""
from typing import Optional

from market_hours import SessionPhase
from signals.base import Direction, Signal, SignalContext, SignalResult
from signals.registry import register_signal
from skew import MIN_OBSERVATIONS, rr25_from_snapshot, skew_percentile
from vrp import observations_before

SIGNAL_ID = "skew_rr25"
VERSION = 1

EXTRAS_KEY = "skew_series"

MODE_CONTRARIAN = "contrarian"
MODE_MOMENTUM = "momentum"


def _history_before(series: list[dict], session_date: str) -> list[dict]:
    return observations_before(series, session_date)


@register_signal(
    SIGNAL_ID,
    version=VERSION,
    description=("25-delta risk reversal ranked as a percentile of its own "
                 "history. An unusually bid put wing is read as capitulation "
                 "(contrarian) or as informed positioning (momentum)."),
    default_params={
        "mode": MODE_CONTRARIAN,
        # Tails only. The middle of a risk-reversal distribution is the
        # structural put bid every index carries and says nothing about today.
        "rich_puts_percentile":  10.0,   # RR unusually LOW  — puts bid
        "rich_calls_percentile": 90.0,   # RR unusually HIGH — calls bid
        "min_observations": MIN_OBSERVATIONS,
        # An intraday reading ranked against a history of closing readings
        # measures the time of day as much as the skew. Off by default; set
        # False deliberately if you have an intraday skew history to match.
        "require_eod_bar": True,
    },
    min_history=0,
)
def skew_rr25(ctx: SignalContext) -> SignalResult:
    """Fires in the tails of the risk-reversal distribution only."""
    series = ctx.extras.get(EXTRAS_KEY) or []
    if not series:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            "no skew series supplied — pass skew.load_skew_history() via extras")

    snap = ctx.snapshot
    if ctx.param("require_eod_bar", True) and snap.session_phase is not SessionPhase.END_OF_DAY:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"bar is {snap.session_phase.value}, not an end-of-day bar — an "
            f"intraday risk reversal ranked against closing readings compares "
            f"times of day, not regimes")

    current = rr25_from_snapshot(snap)
    if current is None:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            "no 25-delta pair on this chain — the wings did not trade, or their "
            "nearest strikes sit further than 0.10 delta from 0.25")

    history = _history_before(series, snap.session_date)
    min_obs = int(ctx.param("min_observations", MIN_OBSERVATIONS))

    features = {
        "rr_25d":       round(current, 4),
        "history_size": len(history),
        "session_date": snap.session_date,
    }

    pct = skew_percentile(history, current, min_obs)
    if pct is None:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"only {len(history)} prior risk-reversal observations, need "
            f"{min_obs} before a percentile means anything",
            features)

    features["rr_percentile"] = round(pct, 2)

    puts_bid  = ctx.param("rich_puts_percentile", 10.0)
    calls_bid = ctx.param("rich_calls_percentile", 90.0)
    mode      = ctx.param("mode", MODE_CONTRARIAN)

    if mode not in (MODE_CONTRARIAN, MODE_MOMENTUM):
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"unknown mode {mode!r} — expected {MODE_CONTRARIAN!r} or "
            f"{MODE_MOMENTUM!r}", features)
    features["mode"] = mode

    if pct <= puts_bid:
        wing = "puts"
        direction = Direction.BULLISH if mode == MODE_CONTRARIAN else Direction.BEARISH
        strength = (puts_bid - pct) / puts_bid if puts_bid else 1.0
    elif pct >= calls_bid:
        wing = "calls"
        direction = Direction.BEARISH if mode == MODE_CONTRARIAN else Direction.BULLISH
        strength = (pct - calls_bid) / (100.0 - calls_bid) if calls_bid < 100 else 1.0
    else:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"risk reversal at the {pct:.0f}th percentile, between {puts_bid} "
            f"and {calls_bid}", features)

    return SignalResult.fire(Signal(
        signal_id = SIGNAL_ID,
        version   = VERSION,
        ts        = ctx.ts,
        symbol    = ctx.symbol,
        direction = direction,
        strength  = max(0.0, min(1.0, strength)),
        features  = features,
        notes     = (f"25-delta risk reversal {current:+.2f} vol points at the "
                     f"{pct:.0f}th percentile of {len(history)} readings — "
                     f"{wing} unusually bid, read as {mode}"),
    ))
