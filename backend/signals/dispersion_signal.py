# optionslens/backend/signals/dispersion_signal.py
"""
Implied-correlation signal (roadmap item 35).

Hypothesis: index options price a correlation risk premium. When implied
correlation is historically rich, index vol is expensive relative to its own
members and selling it pays; when it is historically cheap, the index is
underpricing how together its members could move.

    rich implied correlation   (high percentile)  -> SHORT_VOL on the index
    cheap implied correlation  (low percentile)   -> LONG_VOL on the index

WHAT THE BACKTEST OF THIS DOES AND DOES NOT MEASURE
    A real dispersion trade has two legs: short index vol, long member vol. The
    backtester trades one symbol and scores a volatility signal on that
    symbol's implied minus realised vol — so it measures the INDEX LEG ALONE.
    That is still the right first question (is index vol overpriced precisely
    when implied correlation is rich?), but a positive result here is not a
    dispersion P&L, and a negative one does not rule the full trade out.

    It is also not independent of `vrp`. When implied correlation is rich,
    index IV is usually rich too, so the two will often fire together. The
    signal correlation report (item 36) measures by how much.

    And ρ is a proxy built from a few large members — see `dispersion.py`. Only
    its percentile against its own history is used.

The series arrives through `extras`, built by
`dispersion.load_dispersion_history`; history is sliced by
`vrp.observations_before`, strictly `<`.
"""
from typing import Optional

from dispersion import MIN_OBSERVATIONS, corr_percentile
from signals.base import Direction, Signal, SignalContext, SignalResult
from signals.registry import register_signal
from vrp import DEFAULT_LOOKBACK, recent_observations

SIGNAL_ID = "dispersion"
VERSION = 1

EXTRAS_KEY = "dispersion_series"


def _todays_row(series: list[dict], session_date: str) -> Optional[dict]:
    for row in reversed(series):
        if row.get("date") == session_date:
            return row
    return None


@register_signal(
    SIGNAL_ID,
    version=VERSION,
    description=("Implied correlation of the index against a basket of its members, "
                 "ranked as a percentile of its own history. Rich correlation "
                 "sells index vol, cheap correlation buys it."),
    default_params={
        "rich_percentile":  80.0,
        "cheap_percentile": 20.0,
        "min_observations": MIN_OBSERVATIONS,
        # Prior readings ranked against (~2 years); see vrp.DEFAULT_LOOKBACK.
        "lookback": DEFAULT_LOOKBACK,
    },
    min_history=0,
)
def dispersion_signal(ctx: SignalContext) -> SignalResult:
    """Trade the index leg of the correlation risk premium."""
    series = ctx.extras.get(EXTRAS_KEY) or []
    if not series:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            "no dispersion series supplied — pass "
            "dispersion.load_dispersion_history() via extras (indices only)")

    session_date = ctx.snapshot.session_date
    today = _todays_row(series, session_date)
    if today is None:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"no implied correlation for {session_date} — the index or a basket "
            f"member has no 30-day IV that day")

    history = recent_observations(series, session_date,
                                  int(ctx.param("lookback", DEFAULT_LOOKBACK)))
    min_obs = int(ctx.param("min_observations", MIN_OBSERVATIONS))

    current = today["implied_corr"]
    features = {
        "implied_corr":   current,
        "index_iv_pct":   round(today["index_iv"] * 100, 3),
        "member_iv_pct":  round(today["mean_member_iv"] * 100, 3),
        "realised_corr":  today.get("realised_corr"),
        "corr_premium":   today.get("corr_premium"),
        "history_size":   len(history),
        "session_date":   session_date,
    }

    pct = corr_percentile(history, current, min_obs)
    if pct is None:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"only {len(history)} prior observations, need {min_obs} before a "
            f"percentile means anything", features)
    features["corr_percentile"] = round(pct, 2)

    rich  = ctx.param("rich_percentile", 80.0)
    cheap = ctx.param("cheap_percentile", 20.0)
    if pct >= rich:
        direction, label = Direction.SHORT_VOL, "rich"
        strength = (pct - rich) / (100.0 - rich) if rich < 100 else 1.0
    elif pct <= cheap:
        direction, label = Direction.LONG_VOL, "cheap"
        strength = (cheap - pct) / cheap if cheap else 1.0
    else:
        return SignalResult.skip(
            SIGNAL_ID, VERSION, ctx,
            f"implied correlation at the {pct:.0f}th percentile, between "
            f"{cheap} and {rich}", features)

    return SignalResult.fire(Signal(
        signal_id = SIGNAL_ID,
        version   = VERSION,
        ts        = ctx.ts,
        symbol    = ctx.symbol,
        direction = direction,
        strength  = max(0.0, min(1.0, strength)),
        features  = features,
        notes     = (f"implied correlation {current:.3f} (index IV "
                     f"{today['index_iv'] * 100:.1f}% vs basket "
                     f"{today['mean_member_iv'] * 100:.1f}%) at the {pct:.0f}th "
                     f"percentile of {len(history)} readings — historically {label}"),
    ))
