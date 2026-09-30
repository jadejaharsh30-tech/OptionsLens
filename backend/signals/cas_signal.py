# optionslens/backend/signals/cas_signal.py
"""
CAS auction-dislocation signal (roadmap item 34).

Hypothesis: part of the gap between the last continuous price and the auction
print is price pressure from flow that had to execute at the close, and price
pressure reverts. So an auction that printed unusually far ABOVE the last
continuous price is a BEARISH setup for the next session, and one unusually far
BELOW is BULLISH. See `cas.py` for why the futures-adjusted measure is the one
that actually isolates that pressure.

This is the one signal here that exploits something new. The auction has
existed since 3 Aug 2026; nothing in the literature has measured it on NSE, and
the history to do so is only now accumulating.

WHEN IT RUNS
    Once per session, on the FIRST post-auction bar (~15:35). Earlier bars have
    no close to measure; later ones would fire the same reading again and
    count one session as several signals. Both the current reading and every
    historical one come from `cas.auction_reading`, so they are the same
    quantity — and the reading uses only bars that exist at 15:35, so a
    backtest cannot see anything the live signal could not.

    The history window must reach back to the last continuous bar (~15:14), so
    it needs about 25 one-minute bars. The default of 60 covers it.

HOW IT IS SCORED
    The claim is overnight, but the bars are intraday, so auto-detection would
    score it on 5-60 minute horizons that all fall after the session has ended.
    It therefore DECLARES daily labelling at registration: horizons of 1, 5 and
    20 sessions from the close, null matched on weekday. The overnight reversal
    is inside the 1-session horizon along with the next day's own noise; the
    matched null is what separates the two.
"""
from signals.base import Direction, Signal, SignalContext, SignalResult
from signals.registry import register_signal
from market_hours import SessionPhase
from cas import MEASURES, MIN_OBSERVATIONS, auction_reading, dislocation_percentile
from vrp import DEFAULT_LOOKBACK, recent_observations

SIGNAL_ID = "cas_dislocation"
VERSION = 1

EXTRAS_KEY = "cas_series"


def _is_first_post_auction_bar(ctx: SignalContext) -> bool:
    snap = ctx.snapshot
    if snap.session_phase is not SessionPhase.POST_CAS:
        return False
    return not any(h.session_phase is SessionPhase.POST_CAS
                   and h.session_date == snap.session_date
                   for h in ctx.history)


@register_signal(
    SIGNAL_ID,
    version=VERSION,
    description=("Closing-auction dislocation: the auction print versus the last "
                 "continuous price, ranked against its own history. An extreme "
                 "print is read as price pressure and faded into the next session."),
    default_params={
        # "raw_bps" has history from the first recorded CAS session;
        # "adjusted_bps" strips out the futures' own move over the window and is
        # the measure the hypothesis is really about, but its history starts
        # with futures capture (item 15). Switch once it has enough.
        "measure": "raw_bps",
        "high_percentile": 90.0,
        "low_percentile":  10.0,
        "min_observations": MIN_OBSERVATIONS,
        # Prior readings ranked against (~2 years); see vrp.DEFAULT_LOOKBACK.
        "lookback": DEFAULT_LOOKBACK,
        # An extreme percentile in a quiet stretch can be a two-basis-point gap,
        # well inside the bid-ask of the underlying.
        "min_abs_bps": 5.0,
    },
    min_history=0,
    label_mode="daily",
)
def cas_dislocation(ctx: SignalContext) -> SignalResult:
    """Fade an unusually large auction print into the next session."""
    snap = ctx.snapshot
    if not _is_first_post_auction_bar(ctx):
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"not the first post-auction bar ({snap.session_phase.value}) — the "
            f"signal reads each session's auction once")

    series = ctx.extras.get(EXTRAS_KEY) or []
    if not series:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            "no CAS series supplied — pass cas.load_cas_history() via extras")

    measure = ctx.param("measure", "raw_bps")
    if measure not in MEASURES:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"unknown measure {measure!r} — expected one of {MEASURES}")

    same_session = [h for h in ctx.history if h.session_date == snap.session_date]
    reading = auction_reading(same_session + [snap])
    if reading is None:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            "no continuous bar before the auction in the history window, or the "
            "session predates CAS — widen history_window to reach 15:14")

    current = reading.get(measure)
    features = {k: reading[k] for k in
                ("raw_bps", "futures_bps", "adjusted_bps", "pre_spot", "auction_print")}
    features.update({"measure": measure, "session_date": snap.session_date})
    if current is None:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"{measure} unavailable today — futures missing on one side of the "
            f"auction, or the front contract rolled at 15:30", features)

    history = recent_observations(series, snap.session_date,
                                  int(ctx.param("lookback", DEFAULT_LOOKBACK)))
    min_obs = int(ctx.param("min_observations", MIN_OBSERVATIONS))
    pct = dislocation_percentile(history, current, measure, min_obs)
    features["history_size"] = sum(1 for r in history if r.get(measure) is not None)
    if pct is None:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"only {features['history_size']} prior sessions with {measure}, need "
            f"{min_obs} before a percentile means anything", features)
    features["percentile"] = round(pct, 2)

    floor = ctx.param("min_abs_bps", 5.0)
    if abs(current) < floor:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"dislocation {current:+.1f} bps is inside the {floor} bps floor",
            features)

    high = ctx.param("high_percentile", 90.0)
    low  = ctx.param("low_percentile", 10.0)
    if pct >= high:
        direction, side = Direction.BEARISH, "above"
        strength = (pct - high) / (100.0 - high) if high < 100 else 1.0
    elif pct <= low:
        direction, side = Direction.BULLISH, "below"
        strength = (low - pct) / low if low else 1.0
    else:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"dislocation at the {pct:.0f}th percentile, between {low} and {high}",
            features)

    return SignalResult.fire(Signal(
        signal_id = SIGNAL_ID,
        version   = VERSION,
        ts        = ctx.ts,
        symbol    = ctx.symbol,
        direction = direction,
        strength  = max(0.0, min(1.0, strength)),
        features  = features,
        notes     = (f"auction printed {current:+.1f} bps ({measure}) {side} the "
                     f"last continuous price, {pct:.0f}th percentile of "
                     f"{features['history_size']} sessions — fading it"),
    ))
