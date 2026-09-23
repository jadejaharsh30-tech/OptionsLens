# OptionsLens Roadmap — Alpha Signals, Backtesting & Trade Management

**This file is the project's durable memory.** It survives conversation compaction.
Read it at the start of every session. Update the checkboxes as work completes.

Goal: turn OptionsLens from a visualisation dashboard into a **quantitative
signal-generation, backtesting, and trade-management platform** with real edge.

---

## Guiding principles (do not violate these)

1. **The same signal code must run in live and in backtest.** A signal function
   that imports `fyers_client` is broken by construction. Signals consume
   `ChainSnapshot` objects; the recorder produces them live, the backtester
   replays them from disk.
2. **Record raw, derive late.** Store raw broker fields (`oi`, `oichp`, `ltp`,
   `bid`, `ask`, `volume`, `prev_oi`, spot, futures). Derive IV/Greeks/GEX at
   read time so the pricing model can be fixed without losing data.
3. **Never delete market data.** The old alert engine pruned `oi_snapshots`
   after 20 minutes. Intraday NSE per-strike OI history cannot be bought back.
4. **A signal is not alpha until it beats a null, net of costs.** Fixed forward
   horizons chosen in advance, random-entry-at-same-time-of-day benchmark,
   explicit spread/slippage/STT model.
5. **Log every signal evaluation, not just the fires.** Otherwise you only ever
   see survivors.
6. **Paper fills before real money.** The trade state machine drives paper fills
   first; live execution stays behind an explicit config flag.

---

## Regulatory context: CAS (Closing Auction Session) — live since 3 Aug 2026

SEBI/NSE replaced the old VWAP closing-price mechanism with a call auction for
**F&O-eligible cash stocks** (Category I).

| Item | Value |
|---|---|
| Live since | 3 August 2026 |
| Scope | F&O-eligible **cash stocks** (in our config: RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK) |
| Continuous cash trading ends | **15:15 IST** for those stocks |
| Auction window | ~15:15 → 15:30, equilibrium price finalised ~15:30–15:35 (randomised) |
| Reference price | VWAP of 15:00–15:15, price band ±3% |
| Derivatives (F&O) trading | continues to **~15:40 IST** |
| Index options (NIFTY/BANKNIFTY) | not directly in CAS, **but** the official index close is built from constituent closes, most of which now come from CAS |

**Verify exact minutes against the live NSE circular before trusting them in
production** — published secondary sources disagree on whether the window ends
15:30 or 15:35. `nseindia.com` is egress-blocked from the dev sandbox.

### What CAS broke, and how it is handled now

- Session gating (was a hardcoded 15:30 close) → `market_hours.is_derivatives_open()`,
  which runs to ~15:40. **Fixed, item 2.**
- The 15:20 daily snapshot fired inside the auction window, recording a stale
  pre-auction print as the day's close → IV snapshot moved to 15:10
  (pre-auction) and the official close is captured separately at 15:50 from the
  exchange daily candle. **Fixed, item 14.**
- Expiry-day settlement for **stock** options now flows from the CAS
  equilibrium price, not the last-30-min VWAP. Max-pain / pinning logic built on
  the old mechanism is stale for stocks. **Still open** — matters before any
  pinning signal is built.
- Classic "pin to max pain in the last 30 minutes" assumed continuous trading
  into the close. For stocks that pressure now compresses into the auction.

### What CAS creates (opportunity)

CAS is ~5 weeks old. The dislocation between the 15:15 pre-auction price and the
CAS equilibrium price is a **brand-new, barely-researched microstructure event**,
and essentially nobody holds a clean options-chain dataset around it. Recording
it from today is both a genuine alpha candidate (item 34) and a portfolio
differentiator. This is another reason the recorder is urgent.

---

## Master checklist — 55 items

### Phase 0 — Foundations & correctness — COMPLETE

- [x] 1. CAS-aware market session module (`market_hours.py`) — session phases, correct close times
- [x] 2. Fix event-loop blocking: `run_symbol_tick` must run via `asyncio.to_thread` (`alert_engine/engine.py`)
- [x] 3. Fractional time-to-expiry to the real expiry timestamp (fixes `T=0` killing IV/Greeks on expiry day)
- [x] 4. Forward-based pricing — Black-76 against a forward implied from the chain's own ATM put-call parity (VIX-style min|C−P| rule). Removed a ~1 vol-point call/put IV gap that varied by strike and was being read as skew
- [x] 5. IV solver robustness — Newton-Raphson with bracketed bisection fallback and a Brenner-Subrahmanyam initial guess, plus a vega-based identifiability gate so the solver returns None rather than a fabricated IV where price is flat in vol
- [x] 6. Unify IV pricing inputs — `chain_pricing.price_for_iv()` (mid, falling back to LTP) is now the single definition used by chain, surface, oi, ivrank and the daily snapshot job
- [x] 7. All three SQLite paths (`DB_PATH`, `ALERT_ENGINE_DB`, `MARKET_DATA_DB`) read from env; docker-compose points them at the mounted volume

### Phase 1 — Data capture (URGENT — every day missed is unrecoverable)

- [x] 8. `ChainSnapshot` / `ChainRow` schema (`recorder/models.py`)
- [x] 9. Append-only snapshot store, WAL mode, **no pruning** (`recorder/store.py`, `market_data.db`)
- [x] 10. Recorder service — async loop, `to_thread` for blocking I/O, minute-aligned cadence (`recorder/service.py`)
- [x] 11. Auto-start recorder on successful token validation (never miss a day by forgetting)
- [x] 12. Recorder control/status API (`routers/recorder.py`)
- [x] 13. Record through the CAS window to ~15:40 + tag every snapshot with session phase
- [x] 14. Official EOD close captured post-CAS at 15:50 via the exchange daily candle, stored separately from intraday LTP. IV snapshot moved 15:20 -> 15:10 so it sits in continuous trading, not inside the auction
- [ ] 15. Capture futures price per symbol (needed for forward-based IV + basis signals)
- [x] 16. Data-quality monitor (`recorder/quality.py`) — per-session completeness, gap detection, missing trading days, CAS-window coverage flag
- [ ] 17. Retention/compaction — parquet export + compression for long-term storage
- [x] 18. Recorder indicator in TopNav — flags 'running but not writing', which a simple on/off light would miss

### Phase 2 — Signal framework — COMPLETE

- [x] 19. `SignalContext`/`SignalResult` contract + versioned registry. Signals consume ChainSnapshot only — no broker import is reachable
- [x] 20. Feature store (`signals/features.py`) — forward-based IV/Greeks, GEX profile, zero-gamma flip, max pain, 25d risk reversal, percentile helper
- [x] 21. Versioned registration; params live in the spec and are swept by the backtester, never hardcoded in signal bodies
- [x] 22. Evaluation log persists every evaluation with its skip reason, so the denominator and the kind-of-silence both survive

### Phase 3 — Backtest engine — COMPLETE

- [x] 23. Snapshot replay with a bounded history window appended only AFTER evaluation — look-ahead is structurally impossible, and tested
- [x] 24. Cost model — spread-aware fills (buy ask / sell bid) plus STT, exchange, SEBI, stamp, GST. One-sided books cannot be filled at all
- [x] 25. Forward returns at 5/15/30/60m + EOD, fixed in advance. Horizons past available data return None rather than truncating
- [x] 26. Matched null — random entries at the same clock times, mirroring the signal's direction mix, seeded for reproducibility
- [x] 27. Per-horizon hit rate, mean/median, t-stat, Sharpe-per-observation, MAE/MFE, max drawdown, with automatic thin-sample warnings
- [x] 28. Chronological and rolling walk-forward splits; test always follows train, windows tile without overlap
- [x] 29. Research page — data-readiness banner first, null column beside every result, verdicts as words not numbers

### Phase 4 — Signals with actual edge (ranked by readiness)

- [ ] 30. **VRP signal** — IV−RV percentile rank. NOTE (corrected 2026-09-11): a VRP
  *reading* is available today (RV comes from Fyers history, IV from the live chain),
  but the *signal* is the percentile rank of that spread against its own history,
  and `atm_iv_history` only builds one row per trading day from the day we started
  running. So this needs ~30 sessions of accumulation before it can be evaluated —
  it is not testable today as originally written
- [x] 31. **GEX regime** — implemented as the first registered signal (`signals/library.py`); trades the flip level, not the raw GEX number. NOT yet validated — needs recorded data
- [ ] 32. **Signed aggressor flow** — Lee-Ready style classification from bid/ask, replacing raw OI%
- [ ] 33. **Term structure & skew** — front/back inversion, 25-delta risk reversal percentile
- [ ] 34. **CAS auction dislocation** — 15:15 price vs CAS equilibrium; new since Aug 2026, unexploited
- [ ] 35. **Dispersion / implied correlation** — index IV vs cap-weighted constituent IV (we already have 5 constituents configured)
- [ ] 36. Signal ensemble + conflict resolution
- [ ] 37. Retire/replace the `SHORT_BUILDUP` heuristic once a measured signal beats it

### Phase 5 — Trade lifecycle & journal — COMPLETE

- [x] 38. Trade state machine with validated transitions — illegal moves raise rather than silently correcting
- [x] 39. Entry gates — spread/OI/volume floors, underlying-drift check (refuses to chase), signal revalidation at fill. All failures collected, not short-circuited
- [x] 40. Risk-budget sizing. Floors to whole lots (rounding up silently exceeds budget); shorts sized off stop distance, never off premium received
- [x] 41. Exit rules: stop, target, trailing giveback, IV-crush (longs only), delta drift, time stop, hard expiry flatten which outranks everything
- [x] 42. Paper fills reuse the BACKTESTER's cost model — if paper were more optimistic, forward and backtest results would diverge for reasons unrelated to the signal
- [x] 43. MTM + position Greeks recomputed from current prices, never carried from entry
- [x] 44. MAE/MFE tracked on every mark while open
- [x] 45. Trades page + journal stats; auto-drafted postmortem flags the MAE-vs-outcome relationship
- [x] 46. Portfolio net Greeks, concentration and short-vega/short-gamma warnings
- [x] 47. Broker abstraction with PaperBroker default. LiveBroker deliberately raises NotImplementedError — wiring real orders is a separate, explicit decision, never an inherited default

### Phase 6 — Notifications

- [x] 48. Channel interface + Dispatcher with dedupe and a severity floor. Channel failures are swallowed: a missed alert must never break position management
- [x] 49. **Telegram** — HTML-escaped, length-capped, lazily imported. `verify` distinguishes a bad token from a bad chat_id. (Inline ack/suppress buttons not yet wired — needs a webhook.)
- [ ] 50. Notification rules — dedupe, quiet hours, severity routing
- [ ] 51. Web push / email fallback
- [ ] 52. Daily EOD digest — signals fired, trades taken, P&L, data-coverage report
- [ ] 53. WhatsApp via Meta Business API or Twilio (heavyweight — last, only if genuinely wanted)

### Phase 7 — Platform hardening

- [ ] 54. Router tests with a mocked Fyers client via `app.dependency_overrides`; backtest + signal unit tests
- [ ] 55. CI (GitHub Actions running pytest), Fyers response caching, recorder supervision/auto-restart, Docker persistence

---

## Progress log

Append one line per session. Keep it terse.

- **2026-09-11 (6)** — All of Phase 5 (38-47) plus notifications 48-49.
  Trade lifecycle is live end-to-end on paper: propose -> size -> entry gates ->
  fill -> mark -> exit rules -> close -> journal, persisted at every step.
  Sizing floors to whole lots, which matters more than it sounds: rounding 0.8
  lots up to 1 silently takes ~25% more risk than the budget allows, every
  time. Shorts are sized off stop distance, since collecting 50 of premium does
  not mean risking 50. Paper fills deliberately reuse the backtester's cost
  model so forward results stay comparable to backtest results. Telegram is the
  first notification channel. LiveBroker exists but refuses to trade.
  55 new tests; 184 pass. Frontend builds clean.
- **2026-09-11 (5)** — Items 14, 16, 18, and all of Phases 2 and 3. The signal
  framework and backtester now exist end-to-end: replay -> signal -> labels ->
  matched null -> verdict. Validated on 4 synthetic sessions, and the validation
  itself is the useful part — a signal firing on 100% of bars scored a 100% hit
  rate and +9.79 bps mean, and the null comparison correctly returned NO_EDGE
  with edge 0.00, because a signal that always fires carries no information. A
  backtester without that column would have called it excellent.
  Also: EOD close now captured post-auction at 15:50 from the daily candle, IV
  snapshot moved to 15:10 (pre-auction), data-quality monitor, recorder health
  indicator in TopNav, and GEX regime registered as the first real signal.
  129 tests pass; frontend builds clean.
- **2026-09-11 (4)** — Items 5 and 7. **Phase 0 complete.** Shared solver core:
  Newton-Raphson with a bracketed bisection fallback and a Brenner-Subrahmanyam
  initial guess. The important part is the vega-based identifiability gate — a
  solver that always converges returns confident wrong answers where price is
  flat in vol (deep ITM 7DTE was solving 26.25% against a true 16%). The gate
  uses the quote's own resolution: if a one-vol-point move shifts the model
  price by less than half a 0.05 tick, the IV is not in the data and we return
  None. Swept 4 expiries x 48 strikes x both types: every non-None answer is now
  exact. DB paths are env-configurable, so the Docker volume finally persists.
  106 tests pass.
- **2026-09-11 (3)** — Items 4 and 6. All IV is now Black-76 against a forward
  implied per expiry from the chain's own put-call parity. Measured on NIFTY-like
  inputs (1.3% dividend yield, 30d, true vol 15%), the old spot-based model
  reported call IV 14.16–14.64% against put IV 15.31–15.62% — a ~1 vol-point gap
  that varied by strike and was indistinguishable from skew on the chart. It is
  now zero to solver tolerance. `chain_pricing.py` gives chain/surface/oi/ivrank
  and the snapshot job one shared definition of the IV input price (mid, falling
  back to LTP). Also raised gamma's stored precision: at 6 dp, far-OTM gamma
  rounded to zero and dropped out of GEX entirely. 94 tests pass.
- **2026-09-11 (2)** — Phase 0 correctness pass. Items 2–3 done. `run_symbol_tick`
  now runs via `asyncio.to_thread` (it was stalling the whole API on every poll).
  `days_to_expiry` moved into `market_hours` as `time_to_expiry`, measured to the
  real 15:30 IST expiry instant with IST-aware `now` — 0DTE IV/Greeks work for the
  first time, and the UTC-host date bug is gone. Removed `oi.py`'s now-harmful
  `max(T, 1/365)` floor. All call sites repointed at `market_hours`, which also
  removes `alert_engine` importing from `routers`. 72 tests pass.
- **2026-09-11** — Roadmap created. Built `market_hours.py` (CAS-aware session phases),
  `recorder/` package (ChainSnapshot, append-only store, async service), recorder
  control API, auto-start on token validation. Items 1, 8–13 done.
  Earlier in session: fixed `no such table: alerts` by initialising the alert-engine
  DB at startup (`main.py` lifespan).

---

## Open questions / decisions to revisit

- **IV history discontinuity.** `atm_iv_history` rows written before 2026-09-11
  were computed spot-based and sit ~1 vol point below forward-based values, so
  IV Rank spanning that boundary is slightly distorted. `iv_snapshots` keeps
  per-strike `ltp`, so a one-off rebuild is possible; otherwise accept the seam
  and note it. Matters for item 30 (VRP).

- **Exact CAS window end (15:30 vs 15:35)** — confirm against the NSE circular.
- **Index options settlement under CAS** — index options aren't directly in CAS,
  but the index close derives from CAS constituent closes. Confirm the exact
  index-options final-settlement methodology before building pinning signals.
- **Fyers futures symbol construction** — needed for item 15 (forward-based IV).
  Format not yet verified; the `futures` column exists but is unpopulated.
- **Storage backend at scale** — SQLite is fine to ~1 GB/yr; revisit parquet +
  DuckDB if we widen strike coverage or add symbols.
- **Recording cadence** — currently 60s. Sub-minute would capture CAS dynamics
  better but multiplies storage and Fyers rate-limit pressure.
