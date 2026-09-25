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

## Master checklist — 63 items

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
- [ ] 15. Capture futures price per symbol (needed for forward-based IV + basis signals). Daily history exists in the bhavcopy archive's `daily_future` table; the live recorder still does not capture it
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

- [~] 30. **VRP signal** — BUILT 2026-09-25 as `signals/vrp_signal.py` (`vrp.v1`),
  over `vrp.py` (join, percentile, loader). 30-day constant-maturity ATM IV minus
  20-session realised vol, ranked as a percentile of its own history. IV and RV
  are joined ON DATE, never by position. Look-ahead is controlled by one tested
  function, `observations_before`, which is strictly `<`.
  **NOT YET SCORED PROPERLY — see the open question on vol-outcome labelling.**
  The backtester scores every signal by SIGNED UNDERLYING RETURN, which is the
  wrong target for a volatility signal: a SHORT_VOL position pays when realised
  vol comes in under implied, not when the index goes up. On 300 synthetic
  sessions it fires 19% of the time and returns NO_EDGE at every horizon, which
  only establishes that VRP does not predict direction — something nobody
  claims. A real verdict needs the vol-outcome labeller
- [x] 31. **GEX regime** — implemented as the first registered signal (`signals/library.py`); trades the flip level, not the raw GEX number. NOT yet validated — needs recorded data
- [ ] 32. **Signed aggressor flow** — Lee-Ready style classification from bid/ask, replacing raw OI%
- [x] 33. **Term structure & skew** — DONE 2026-09-25. Two signals, both ranked
  against their own history and both fed through `extras` because neither is
  visible in a single snapshot.
  `term_structure.v1` (`term_structure.py` + `signals/term_structure_signal.py`):
  60d minus 30d constant-maturity ATM IV, both legs interpolated in total
  variance so the weekly roll cannot manufacture a slope. Steep contango →
  SHORT_VOL (the front rolls down), genuine inversion → LONG_VOL. A percentile
  alone is not enough in either tail: an index curve is in contango nearly
  always, so its 20th percentile is still a normal curve, and LONG_VOL
  additionally requires the slope to be ≤ 0.
  `skew_rr25.v1` (`skew.py` + `signals/skew_signal.py`): 25-delta risk reversal
  percentile. The direction is genuinely contested, so `mode` selects
  contrarian (bid put wing = capitulation → BULLISH) or momentum (= informed
  positioning → BEARISH), recorded in the run's params. EOD bars only by
  default — an intraday reading ranked against closing readings measures the
  time of day.
  **Two cautions on any result.** (a) `term_structure` is NOT independent of
  `vrp`: both are driven by the same calm/stress regime, so an ensemble
  counting them as two confirmations counts the regime twice. (b) The 60-day
  leg is the fragile one — it needs a traded expiry beyond two months, which
  monthly-only symbols often lack, so its percentile can end up conditioned on
  the far month having traded. `term_structure.coverage()` reports that per
  symbol and the API surfaces it as a note
- [ ] 34. **CAS auction dislocation** — 15:15 price vs CAS equilibrium; new since Aug 2026, unexploited
- [ ] 35. **Dispersion / implied correlation** — index IV vs cap-weighted constituent IV (we already have 5 constituents configured)
- [ ] 36. Signal ensemble + conflict resolution
- [~] 37. Retire/replace the `SHORT_BUILDUP` heuristic once a measured signal beats it.
  First live run (2026-09-24) fired three BEARISH/BUY-PE alerts on NIFTY 23050-23150 CE
  at 14:39-14:40 during an up-trending session. The rule reads "call OI up + call premium
  down" as call writing, but four days before a weekly expiry the premium falls from theta
  alone, and the index had just stalled after a rally. It also reports OI change against
  a tiny prior-settle base (+2041%).
  **PORTED 2026-09-24** as `signals/oi_buildup.py` (`oi_short_buildup.v1`). The engine's
  mutable `pending_spikes` is reconstructed from the history window, so the signal is pure
  and replayable; `test_oi_buildup.py` asserts the classifiers match the live engine's
  across every quadrant, since the signal re-implements rather than imports them (the
  engine reaches a broker client at module scope).
  Still OPEN: no verdict yet. The recorder only began collecting on 2026-09-24 (the
  auth-validate bug had stopped it auto-starting), so there is ~1 session. On a synthetic
  up-trending session with theta decay it fires on 96% of bars, always BEARISH, 0% hit
  rate and mean -14.7 bps at 60m, with edge 0.00 against the null — the shape the live
  false positive predicted, but synthetic data proves nothing about the market. Re-run
  once real sessions accumulate

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

- [x] 54. Router tests — 30 covering auth/validate, recorder, backtest, trades, notify, alert-engine, chain, oi, expiries, with the broker faked via `dependency_overrides` + monkeypatch. Includes a regression for the auth bug that rejected every valid token. Found that `start_scheduler` was not safe to call twice; now guarded
- [ ] 55. CI (GitHub Actions running pytest), Fyers response caching, recorder supervision/auto-restart, Docker persistence

### Phase 8 — Education & explainability layer

**Why this is a phase and not a README section.** Every number this app shows is
the output of a deliberate methodological choice, and most of those choices are
invisible in the number itself. IV Rank is a percentile rather than a range
because front-expiry IV is mostly the weekly roll. The IV solver returns nothing
where the price is flat in vol. A backtest verdict reads `NO_EDGE` rather than a
Sharpe ratio because a number invites talking yourself into it. A user who does
not know any of that reads the same screen and draws confident wrong conclusions
— which is the exact failure the codebase spends its effort preventing
internally. The education layer extends that discipline to the interface.

**The governing constraint: it must be derived from the code, not maintained
beside it.** The README already drifted (it describes three modules and "36 unit
tests"), and stale teaching material is worse than none — it teaches the wrong
thing with the same confidence. So every item below either reads from the code
or is pinned to it by a test.

- [ ] 56. **Single-source concept glossary** — one structured definition per term
  (`docs/CONCEPTS.md` plus a machine-readable table the API serves), each entry
  carrying: what it is, **how THIS project computes it**, the unit, and the trap.
  Same rule as "ATM IV has one definition": the docs page and the UI tooltip must
  read the same entry or they will disagree within a month
- [ ] 57. **Inline explainers on every metric** — an affordance next to each number
  that pulls its glossary entry. The caveat is the part that matters; "IV Rank:
  percentile of 30-day constant-maturity IV over 252 sessions" teaches nothing
  that "IV Rank" did not, while "not (IV−low)/(high−low) — one spike would pin a
  year of readings near zero" teaches the actual idea
- [ ] 58. **"Why is this empty?"** — the highest-value piece and the one unique to
  this codebase. The app deliberately returns `None` in a dozen places: an
  unidentifiable IV, an unbracketed constant-maturity tenor, a missing far leg, a
  25-delta wing that did not trade, a thin history, `INSUFFICIENT_DATA`. Today
  each of those renders as a blank cell. Every one should name the guard that
  produced it and what would change it. Mostly a **surfacing** job, not new logic:
  the strings already exist as `SignalResult.reason`, the router notes and the
  coverage reports
- [ ] 59. **Task-oriented runbooks** — separate from reference material, because
  "what is VRP" and "how do I run my first backtest" are different questions.
  Minimum set: first backtest end to end; the daily operating routine (validate
  token → recorder runs → check coverage); signal → paper trade → journal; adding
  an underlying; what to do when the recorder missed a day
- [ ] 60. **Methodology write-up** — the quant reasoning consolidated in one place:
  why Black-76 against an implied forward and never spot, why total-variance
  interpolation, why a vega identifiability gate, why matched nulls, why
  percentile rank, why paper reuses the backtester's cost model. This is also the
  portfolio-facing artifact — it is the document that shows the reasoning rather
  than the wiring. It is already written, scattered across docstrings and this
  file; consolidating is cheap
- [ ] 61. **Worked examples using this project's own measured numbers** — the
  strongest teaching device available here, because the measurements are real and
  they prove the fix mattered: front-expiry IV swinging 6.80 vol points against
  2.20 for constant-maturity over the same 29 sessions; a 7DTE deep-ITM contract
  solving 26.25% against a true 16%; config lot sizes drifted to NIFTY 75 /
  BANKNIFTY 15 against an exchange 65 / 30, doubling intended risk. Each one
  teaches the concept and demonstrates the consequence
- [ ] 62. **"Reading a result honestly"** — statistics literacy specific to this
  tool. What `NO_EDGE` and `INSUFFICIENT_DATA` actually assert; why the null
  column is the load-bearing one; why 20 sessions is a plumbing check; why
  correlated signals are not independent confirmations (`vrp` and
  `term_structure` share a regime driver); why running both `skew_rr25` modes
  over one sample is two tests reported as one
- [ ] 63. **Drift guards** — tests that pin the teaching material to the code, so
  this phase cannot rot the way the README did. At minimum: every registered
  signal has a glossary entry; every glossary entry names a symbol that exists;
  every documented endpoint is routable. The generic `_build_extras` test in
  `test_routers.py` is the pattern

---

## Progress log

Append one line per session. Keep it terse.

- **2026-09-25 (15)** — Item 33: term structure and skew. `term_structure.v1`
  ranks the 60d−30d constant-maturity slope; `skew_rr25.v1` ranks the 25-delta
  risk reversal. Both receive their history through `extras`, like `vrp` — a
  snapshot holds ONE expiry, so the curve is not in it by construction, and the
  EOD adapter materialises only the front chain, so it is not in the replay
  window either. Added `recorder.store.last_snapshot_of_session` so a daily
  series can be built without walking every minute of every session.
  `_build_extras` is now three independent passes, so one broken leg cannot
  blank the others, and the skew pass (a chain-wide IV solve per session) is
  built only for the signal that consumes it. Two design points worth keeping:
  the term-structure tails need an ABSOLUTE guard as well as a percentile,
  because a normally-contango curve's 20th percentile is not an inversion; and
  the skew direction is a parameter with an explicit warning that testing both
  modes on one sample is two tests reported as one. 417 tests pass.

- **2026-09-25 (14)** — Cross-session history windows, router tests, and the
  gap that made item 30 unusable from the UI. `/api/backtest/run` now builds the
  VRP and IV series server-side via `vrp.load_vrp_history` and passes them as
  extras: without this the `vrp` signal appeared in the dropdown and skipped
  every bar with "no VRP series supplied". It also returns a note saying how
  many paired observations exist, so an empty history is visible rather than
  silent. 375 tests pass.

- **2026-09-25 (13)** — Vol-outcome labelling, so volatility signals can finally
  be scored on volatility. `vol_labels.py` measures implied at entry against vol
  realised AFTERWARDS - deliberately not the contemporaneous spread the VRP
  signal reads as input, which would be circular. Mode is chosen from the fired
  signals' direction. Note `gex_regime` emits SHORT_VOL/LONG_VOL and is now
  scored on vol too, which is the right target for a dealer-gamma-regime signal.
  342 tests pass.

- **2026-09-25 (12)** — Item 30 built, and the RV dating bug fixed.
  `rv_series_from_dated_closes` takes dated closes from `spot_history` instead
  of reconstructing dates by counting weekdays back from today, which ignored
  exchange holidays and shifted RV against IV after every one. `vrp.py` joins
  the two ON DATE so a missing reading is a gap rather than an invisible
  one-day offset. `/api/ivrank` now returns `rv_dates_exact` so an empty
  `spot_history` shows as an empty chart rather than a misaligned one.
  323 tests pass.
  **Immediately surfaced the next gap:** the backtester scores signals by signed
  underlying return, which cannot evaluate a volatility signal at all. VRP is
  built and correct; the scoring target is wrong for it. See open questions.

- **2026-09-25 (11)** — Cross-session labelling, which unblocks all EOD research.
  `HORIZONS_DAYS = (1, 5, 20)` counted in trading sessions, a weekday-matched
  null (`sample_null_sessions`), and mode selection driven by the data rather
  than a flag. On EOD-shaped history the backtester previously scored n=0 at
  every horizon with a degenerate self-comparing EOD label; it now returns real
  1d/5d/20d statistics against a matched null. 301 tests pass.
  Note for future sessions: importing a fixture from another test module
  re-executes its `@register_signal` decorators under a second module identity
  (pytest imports test files without a package prefix) and trips the registry's
  duplicate guard only when the whole suite runs. Keep test fixtures local.

- **2026-09-24 (10)** — Item 37 ported and the bhavcopy adapter built.
  `signals/oi_buildup.py` reconstructs the alert engine's mutable
  `pending_spikes` from the history window so the rule is pure and replayable;
  its classifiers are re-implemented (the engine reaches a broker at module
  scope) and tested against the engine's own across every quadrant. No verdict
  yet — the recorder has ~1 session. `bhavcopy/snapshots.py` rebuilds exchange
  EOD rows as `ChainSnapshot`s tagged `SessionPhase.END_OF_DAY`, materialised
  into a SEPARATE database so one-per-day bars never interleave with the
  recorder's one-per-minute bars. A rebuilt chain solves back to the vol it was
  generated with, which is the real proof the adapter is faithful. 287 tests.
  **Found doing it:** the forward-return labeller is intraday-shaped. On EOD
  data every intraday horizon returns n=0 and `eod` is degenerate (entry bar and
  last bar of the session are the same bar), so daily signals cannot be scored
  until `backtest/labels.py` grows cross-session horizons. See open questions.

- **2026-09-24 (9)** — Routine updates for exchange history. `--to today`, and a missing
  file from the last four days is retried instead of being recorded as a holiday (running
  the update before NSE published the day's file used to lose that day permanently).
  Runbook in `docs/BHAVCOPY.md`. 256 tests pass.
- **2026-09-24 (8)** — First run on Windows, from the user's laptop, exposed three bugs
  that Linux had hidden. Token validation always failed ("no running event loop": a sync
  endpoint cannot create the recorder's asyncio task), so the recorder had never actually
  auto-started; now async. `/api/ivrank` crashed on Windows via a dead `strftime("%s")`
  line in `fetch_historical_prices`; removed. Two tests wrote to `/tmp`; now use the OS
  temp dir. Recorder confirmed running live for the first time. 252 tests pass.
- **2026-09-23 (7)** — Exchange EOD history and a corrected IV Rank. New
  `bhavcopy/` package: a stdlib-only NSE archive downloader (both schema eras,
  verified against live files) and an importer into the app's own stores.
  545 trading days from 2024-07-08 downloaded locally, 2.8M option rows, zero
  gaps. Measured on real files: untraded contracts publish yesterday's close as
  today's, expiry-day settlement is the index level, OI is in shares. IV Rank
  was ranking front-expiry IV, which is mostly the weekly roll (6.8 vs 2.2 vol
  point range over 29 sessions), and its history query mixed tenors once a date
  had more than one expiry. It now ranks 30-day constant-maturity IV as a
  percentile. Lot sizes come from exchange data per contract: config had NIFTY
  75 / BANKNIFTY 15 against 65 / 30, so trade sizing took twice the intended
  risk on BANKNIFTY. Every configured stock's lot size had also drifted except
  ICICIBANK's, which is unverified: RELIANCE 250 -> 500, TCS 150 -> 225,
  HDFCBANK 550 -> 650, INFY 300 -> 400. The 29 dates with no archive file all
  match exchange holidays. 246 tests pass; frontend builds clean.

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

- ~~Vol signals need a vol outcome~~ **DONE 2026-09-25.** `backtest/vol_labels.py`
  scores SHORT_VOL / LONG_VOL by (IV at entry - subsequently realised vol) in
  VOL POINTS, signed so positive always means the signal was right. Horizons
  5/10/20 sessions; the 20-session one is tenor-matched to a 30-day implied and
  is the honest headline. The mode is read from the fired signals' direction,
  not a flag. `BacktestRun.unit` carries "bps" or "vol_points" so the two are
  never read as comparable.
  **Methodological caution found while validating it, and it applies to REAL
  results too:** implied vol sits on BOTH sides of a VRP test - in the signal
  input (IV minus trailing RV) and in the outcome (IV minus future RV). That
  shared component makes a synthetic null very hard to construct, and it means a
  genuine backtest can show edge partly because wide premiums mean-revert rather
  than because the signal times anything. Validation: a fixture where rich
  premiums genuinely precede calm scored +12.43 vol points (t=18.85); a fixture
  with constant IV, where a high VRP only means trailing vol was low, correctly
  returned NO_EDGE with a slightly NEGATIVE edge at 5d. The labeller
  discriminates and is not biased toward finding edge.

- ~~History windows do not span sessions~~ **DONE 2026-09-25.** `replay_range`
  carries the window between sessions, decided from the data: one bar per
  session carries, intraday does not. Carrying intraday would let an
  OI-velocity rule match a "spike" across the overnight gap, where open
  interest has been restated against a new settlement. Appending still happens
  only AFTER evaluation, so the look-ahead guarantee is unchanged and tested.

- ~~Daily-horizon labels are missing~~ **DONE 2026-09-25.** `+1d/+5d/+20d`
  counting trading sessions (so a holiday cannot silently shorten a horizon),
  with a null matched on WEEKDAY rather than clock time — clock matching is
  meaningless when every bar is a close, and weekday captures the weekly expiry
  cycle empirically without hardcoding a rule NSE has already changed once.
  The engine picks the mode from the data (one bar per session -> daily), not
  from a flag, so daily bars cannot be scored on intraday horizons by mistake.
  `label_mode` can still be forced. Items 30, 33 and 35 are now scoreable on
  EOD history.
  Remaining nuance: days-to-expiry would be a sharper match than weekday, but
  needs an expiry calendar the labels do not carry.

- **Found on the first Windows run (2026-09-24), still open:**
  - The term-structure chart (`routers/surface.py`) picks its own ATM IV (call IV, else
    put IV, at the strike nearest spot) instead of `chain_pricing.atm_iv_for_chain`, so it
    can disagree with IV Rank's per-expiry figures.
  - Fyers load: the recorder, alert engine and dashboard now all poll at once. Watch for
    rate-limit errors in the backend log before adding more pollers.
  - Config `strike_step` for the stocks is unverified after bonus issues; only the alert
    engine uses it. ICICIBANK's lot size is also unverified.

- **IV history discontinuity.** `atm_iv_history` rows written before 2026-09-11
  were computed spot-based and sit ~1 vol point below forward-based values, so
  IV Rank spanning that boundary is slightly distorted. `iv_snapshots` keeps
  per-strike `ltp`, so a one-off rebuild is possible; otherwise accept the seam
  and note it. Matters for item 30 (VRP).
  The bhavcopy importer never overwrites, so on any date that already has a
  live row, that row stays and the importer only adds the other expiries. Those
  dates mix one live reading (mid-based, and spot-based before 2026-09-11) with
  close-based ones. `atm_iv_history.source` identifies them if they need
  replacing.

- **Pre-July-2024 history.** The archive downloads back to 2000 and has options
  from 2001, but older files need two fixes first: some years (2003, 2005, 2006
  seen) spell the option-type column differently, and no file before 2024-07-08
  carries an underlying price, so spot must come from futures. Signals built on
  monthly expiries (VRP, term structure) could usefully go back to ~2008, which
  adds the 2008 crash and March 2020. Weekly-expiry signals only have history
  from 2016 (BANKNIFTY) and 2019 (NIFTY).

- **Stock IV history coverage — measured, acceptable.** Stocks list monthly
  expiries only, so a 30-day reading needs the second month to trade. Over
  540 dates the five configured stocks had a reading on 89-100% of them
  (ICICIBANK 89%, RELIANCE 99.8%); FINNIFTY had almost none. Revisit only if
  an unconfigured, thinner name is added.
- **Highest readings are a real event.** NIFTY's 30-day IV peaks at 29.0% and
  BANKNIFTY's at 32.5%, both on 2026-03-30, with the top six readings for NIFTY,
  BANKNIFTY and INFY all falling between 2026-03-19 and 2026-04-06. A data fault
  would be isolated to one day and one symbol. INFY's other top reading is
  2025-04-07, the April 2025 global selloff. Worth confirming against India VIX.

- **Exact CAS window end (15:30 vs 15:35)** — confirm against the NSE circular.
- **Index options settlement under CAS** — index options aren't directly in CAS,
  but the index close derives from CAS constituent closes. Confirm the exact
  index-options final-settlement methodology before building pinning signals.
  Partial answer: on expiry day the bhavcopy's `SttlmPric` for every index
  option contract is the official settlement level of the index itself, so the
  settlement value per expiry is available historically.
- **Fyers futures symbol construction** — needed for item 15 (forward-based IV).
  Format not yet verified; the `futures` column exists but is unpopulated.
- **Storage backend at scale** — SQLite is fine to ~1 GB/yr; revisit parquet +
  DuckDB if we widen strike coverage or add symbols.
- **Recording cadence** — currently 60s. Sub-minute would capture CAS dynamics
  better but multiplies storage and Fyers rate-limit pressure.
