# optionslens/backend/backtest/
"""
Backtest engine.

Replays recorded ChainSnapshots through the same signal code that runs live,
labels each signal with forward returns at horizons fixed in advance, charges
realistic costs, and compares the result against a null built from random
entries at the same times of day.

The last part is the one people skip. A signal that fires at 09:20 and "works"
may only be capturing the opening-auction drift that any random 09:20 entry
would have caught. Without the null you cannot tell the difference, and the
backtest tells you nothing.

    costs.py      Indian F&O transaction costs + spread/slippage
    labels.py     forward returns at fixed horizons
    benchmark.py  random-entry-at-same-time-of-day null
    metrics.py    hit rate, expectancy, Sharpe, drawdown, MAE/MFE
    replay.py     snapshot replay driving the signal registry
    engine.py     orchestration
    walkforward.py out-of-sample splits
"""
