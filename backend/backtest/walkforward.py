# optionslens/backend/backtest/walkforward.py
"""
Out-of-sample splitting.

Any parameter chosen by looking at results is fitted to those results. The only
defence is to choose on one slice of time and measure on a later one, never
overlapping and never shuffled — intraday data is autocorrelated, so a random
split leaks tomorrow into today and reports a fantasy.

Splits are strictly chronological and the test window always follows the train
window in real time.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Split:
    """One train/test pair, by session date."""
    index:  int
    train:  tuple[str, ...]
    test:   tuple[str, ...]

    @property
    def summary(self) -> str:
        return (f"split {self.index}: train {self.train[0]}..{self.train[-1]} "
                f"({len(self.train)}d) -> test {self.test[0]}..{self.test[-1]} "
                f"({len(self.test)}d)")


def chronological_split(dates: list[str], train_frac: float = 0.6) -> Optional[Split]:
    """
    A single train/test cut. The simplest honest thing to do with a short series.
    """
    ordered = sorted(dates)
    if len(ordered) < 4:
        return None
    cut = max(1, int(len(ordered) * train_frac))
    if cut >= len(ordered):
        return None
    return Split(index=0, train=tuple(ordered[:cut]), test=tuple(ordered[cut:]))


def rolling_splits(dates: list[str], train_days: int, test_days: int,
                   step_days: Optional[int] = None) -> list[Split]:
    """
    Rolling walk-forward windows.

    Each split trains on `train_days` sessions and tests on the `test_days`
    that immediately follow. With `step_days` equal to `test_days` (the default)
    the test windows tile the series without overlapping, so every out-of-sample
    day is used exactly once and results can be pooled without double counting.
    """
    ordered = sorted(dates)
    step = step_days or test_days
    splits: list[Split] = []

    start, idx = 0, 0
    while start + train_days + test_days <= len(ordered):
        splits.append(Split(
            index = idx,
            train = tuple(ordered[start:start + train_days]),
            test  = tuple(ordered[start + train_days:start + train_days + test_days]),
        ))
        start += step
        idx += 1

    return splits


def describe_data_sufficiency(dates: list[str], train_days: int,
                              test_days: int) -> dict:
    """
    Whether there is enough history to walk forward at all.

    Called before running so a thin dataset produces an explicit "not yet"
    rather than one meaningless split presented as a result.
    """
    n = len(dates)
    needed = train_days + test_days
    return {
        "sessions_available": n,
        "sessions_needed":    needed,
        "sufficient":         n >= needed,
        "possible_splits":    max(0, (n - needed) // max(1, test_days) + 1),
        "note": (
            f"Have {n} sessions, need {needed} for one train/test cycle."
            if n < needed else
            f"{n} sessions available."
        ),
    }
