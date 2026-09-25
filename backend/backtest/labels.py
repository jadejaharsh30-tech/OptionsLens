# optionslens/backend/backtest/labels.py
"""
Forward-return labelling.

Horizons are declared as a module constant and fixed in advance. This is the
whole discipline: if you choose the horizon after seeing which one worked, you
have selected on noise, and the number you report is not an estimate of
anything. Add horizons deliberately and note the change in the roadmap — never
tune them per signal.

Labels are computed against the UNDERLYING, not the option. Option P&L is
path-dependent and contaminated by theta and vol moves, so as a measure of
whether a *directional* signal was right it is hopeless. Whether the resulting
trade is profitable after costs is a separate question the cost model answers.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# Fixed in advance. Do not tune per signal.
HORIZONS_MIN = (5, 15, 30, 60)
EOD_LABEL = "eod"

# Cross-session horizons, in trading sessions, for daily-frequency data.
#
# Exchange EOD history carries one bar per session, so every intraday horizon
# scores n=0 against it and the EOD label compares a bar with itself. Daily
# signals (VRP, term structure, dispersion) need returns measured ACROSS
# sessions instead. Fixed in advance for the same reason the intraday ones are.
HORIZONS_DAYS = (1, 5, 20)


@dataclass
class ForwardReturns:
    """Returns in basis points at each horizon, plus MAE/MFE along the way."""
    ts:      str
    symbol:  str
    spot_at_signal: float
    returns_bps: dict[str, Optional[float]]
    mae_bps: dict[str, Optional[float]]   # worst adverse excursion
    mfe_bps: dict[str, Optional[float]]   # best favourable excursion

    def label(self, horizon: str) -> Optional[float]:
        return self.returns_bps.get(horizon)


def _bps(entry: float, later: float) -> float:
    return (later / entry - 1.0) * 10_000.0


def compute_forward_returns(
    index: int,
    timestamps: list[str],
    spots: list[float],
    symbol: str,
    horizons_min: tuple[int, ...] = HORIZONS_MIN,
    direction: int = 1,
) -> ForwardReturns:
    """
    Label the observation at `index` using only data strictly after it.

    Args:
        index: position in the series being labelled
        timestamps/spots: the full session series, ascending
        direction: +1 for a long view, -1 for short. MAE/MFE are expressed in
                   the direction of the trade, so "adverse" always means
                   against the position regardless of side.

    Returns None for a horizon that runs past the end of available data rather
    than truncating to the last bar — a 60-minute label built from 12 minutes
    of data is a different statistic wearing the same name.
    """
    entry_ts   = datetime.fromisoformat(timestamps[index])
    entry_spot = spots[index]

    returns: dict[str, Optional[float]] = {}
    mae: dict[str, Optional[float]] = {}
    mfe: dict[str, Optional[float]] = {}

    last_ts = datetime.fromisoformat(timestamps[-1])

    for h in horizons_min:
        target = entry_ts.timestamp() + h * 60
        if last_ts.timestamp() < target:
            # Not enough forward data for an honest label at this horizon.
            key = f"{h}m"
            returns[key] = mae[key] = mfe[key] = None
            continue

        path = []
        end_value = None
        for j in range(index + 1, len(timestamps)):
            t = datetime.fromisoformat(timestamps[j]).timestamp()
            if t > target:
                break
            path.append(spots[j])
            end_value = spots[j]

        key = f"{h}m"
        if end_value is None or not path:
            returns[key] = mae[key] = mfe[key] = None
            continue

        excursions = [direction * _bps(entry_spot, p) for p in path]
        returns[key] = round(direction * _bps(entry_spot, end_value), 2)
        mae[key] = round(min(excursions), 2)
        mfe[key] = round(max(excursions), 2)

    # End of session — always available, no truncation concern.
    returns[EOD_LABEL] = round(direction * _bps(entry_spot, spots[-1]), 2)
    eod_path = [direction * _bps(entry_spot, p) for p in spots[index + 1:]] or [0.0]
    mae[EOD_LABEL] = round(min(eod_path), 2)
    mfe[EOD_LABEL] = round(max(eod_path), 2)

    return ForwardReturns(
        ts = timestamps[index],
        symbol = symbol,
        spot_at_signal = entry_spot,
        returns_bps = returns,
        mae_bps = mae,
        mfe_bps = mfe,
    )


def horizon_keys(horizons_min: tuple[int, ...] = HORIZONS_MIN) -> list[str]:
    return [f"{h}m" for h in horizons_min] + [EOD_LABEL]


def daily_horizon_keys(horizons_days: tuple[int, ...] = HORIZONS_DAYS) -> list[str]:
    return [f"{d}d" for d in horizons_days]


def compute_daily_forward_returns(
    index: int,
    dates: list[str],
    closes: list[float],
    symbol: str,
    horizons_days: tuple[int, ...] = HORIZONS_DAYS,
    direction: int = 1,
) -> ForwardReturns:
    """
    Label a daily observation using only sessions strictly after it.

    The cross-session twin of `compute_forward_returns`. Horizons count TRADING
    SESSIONS, not calendar days, so a long weekend or an exchange holiday does
    not silently shorten a horizon — `dates` is the list of sessions we actually
    hold, in order, and +1d means the next one of those.

    Args:
        index: position in the session series being labelled
        dates/closes: the full session series, ascending, one close per session
        direction: +1 long, -1 short. MAE/MFE are expressed in the direction of
            the trade, so "adverse" always means against the position.

    A horizon running past the end of the data returns None rather than being
    truncated to the last available session: a 20-session label built from 4
    sessions is a different statistic wearing the same name.
    """
    entry_close = closes[index]

    returns: dict[str, Optional[float]] = {}
    mae: dict[str, Optional[float]] = {}
    mfe: dict[str, Optional[float]] = {}

    for d in horizons_days:
        key = f"{d}d"
        end = index + d
        if end >= len(closes):
            returns[key] = mae[key] = mfe[key] = None
            continue

        path = closes[index + 1:end + 1]
        excursions = [direction * _bps(entry_close, p) for p in path]
        returns[key] = round(direction * _bps(entry_close, closes[end]), 2)
        mae[key] = round(min(excursions), 2)
        mfe[key] = round(max(excursions), 2)

    return ForwardReturns(
        ts=dates[index],
        symbol=symbol,
        spot_at_signal=entry_close,
        returns_bps=returns,
        mae_bps=mae,
        mfe_bps=mfe,
    )
