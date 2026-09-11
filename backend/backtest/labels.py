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
