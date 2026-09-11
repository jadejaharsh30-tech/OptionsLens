# optionslens/backend/recorder/
"""
Market data recorder.

Captures full option-chain snapshots to an append-only store so that signals
can be researched and backtested later. NSE intraday per-strike OI history
cannot be purchased retroactively — data not recorded today is gone forever.

The recorder is deliberately decoupled from the alert engine: it knows nothing
about signals, and signals know nothing about Fyers. Both sides meet at
`ChainSnapshot`, which is what makes live and backtest share one code path.
"""
