# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read this first

**`docs/ROADMAP.md` is the project's durable memory.** It holds the 63-item
phased checklist (signal research → backtesting → trade lifecycle → notifications
→ education layer),
the guiding principles, the CAS regulatory findings, and a progress log. Read it
at the start of every session and tick items off as they land.

## Working with the user

- The user runs the backend on **Windows, in `cmd.exe`** (not PowerShell), from
  a `.venv`, sometimes on a work laptop whose data lacks ICICIBANK. Give
  commands in cmd syntax: `^` for line continuation, double quotes outside and
  single quotes inside for `python -c`, `set VAR=value` for environment
  variables.

## What this is

NSE options market intelligence dashboard: FastAPI backend + React/Vite frontend. Live data comes from the Fyers API v3 via a user-supplied daily access token; all quant math (Black-Scholes, IV, Greeks, GEX, SVI) is implemented from scratch in pure Python — no numpy/scipy/quant libraries.

## Commands

### Backend (Python 3.11, run from `backend/`)

```bash
pip install -r requirements.txt          # includes pytest
uvicorn main:app --reload --port 8000    # API docs at http://localhost:8000/docs

python -m pytest tests/                              # all tests
python -m pytest tests/test_iv_engine.py             # one file
python -m pytest tests/test_iv_engine.py -k parity   # one test by keyword

python -m term_structure                 # 30d/60d leg coverage per underlying
python -m futures                        # front-month basis / implied carry per underlying
python -m dispersion                     # implied-correlation basket per index

# Backtest over the bhavcopy history (the Research page only sees recorder data)
python -m backtest.cli run --signal vrp --symbol NIFTY
python -m backtest.cli correlation --symbol NIFTY --signals vrp,term_structure,dispersion
python -m backtest.cli run --signal vrp --symbol NIFTY --to 2024-07-05   # out-of-sample years only

# Before a long pre-2024 download (home connection): how each year's file parses
python -m bhavcopy.download --probe 2001-2024 --stocks --symbols NIFTY,BANKNIFTY
```

532 tests, none needing a Fyers token or network: the engines, `chain_pricing`, `market_hours`, `recorder`, `snapshot_store`, `eod_vol`, `lot_sizes`, `bhavcopy` (importer end to end on a synthetic Black-76 archive), `signals`/`backtest`, `term_structure`/`skew`, `futures`, `cas`, `dispersion`, signal correlation, `trading`, `notify`, `alert_engine/percentile_threshold`. Routers are covered by `test_routers.py` (fake broker via monkeypatch + `dependency_overrides`, module-scoped client because the lifespan starts a scheduler); `fyers_client` itself is still untested.

### Frontend (run from `frontend/`)

```bash
npm install
npm run dev      # Vite dev server on http://localhost:5173
npm run build
```

No frontend tests or linter are configured.

### Docker

```bash
docker compose up --build   # frontend on :80 (nginx), backend on :8000; SQLite persisted to ./data
```

## Architecture

### Request flow and auth

The Fyers access token is the backbone of every request:

- Frontend stores the token in `localStorage`; the Axios instance (`frontend/src/api/client.js`) injects it as a `Bearer` header on every call. In dev, the Vite proxy forwards `/api/*` and `/health` to `localhost:8000` — never hardcode backend URLs in components.
- Backend is stateless by design: `auth.py` provides the `get_token` / `get_fyers_client` FastAPI dependencies that extract the token per request; `fyers_client.py` builds a fresh `FyersModel` each time. No token is ever written to disk.
- `/api/auth/validate` (main.py) doubles as token registration: it stores the token in-memory in `scheduler.py` so the daily 15:10 IST snapshot cron job can use it. If no token was validated that day, the snapshot silently skips.

### Backend layering

- `config.py` — single source of truth: `UNDERLYINGS` (Fyers symbol strings, fallback lot sizes, strike steps), `RISK_FREE_RATE`, IV solver params, the four DB paths (`DB_PATH`, `ALERT_ENGINE_DB`, `MARKET_DATA_DB`, `NSE_EOD_DB`).
- Pure-math engines (`iv_engine.py`, `gex_engine.py`, `svi_engine.py`, `realized_vol.py`, `forward_engine.py`) take plain floats/lists and know nothing about Fyers or FastAPI. Keep them dependency-free — this is what makes them unit-testable.
- **All IV is Black-76 against an implied forward, never spot.** `chain_pricing.py` is the shared glue: `implied_forward_for_chain()` recovers the forward from the chain's own put-call parity (VIX-style min |C−P| strike), and `price_for_iv()` defines the input price (bid-ask mid, falling back to LTP). chain, surface, oi, ivrank and `scheduler.py` all go through it. Do not reintroduce `implied_volatility(S=spot, ...)` in a router: pricing off spot inflates call IVs and deflates put IVs by roughly the dividend yield (~1 vol point on NIFTY), which shows up as fake skew and breaks put-call parity in our own numbers.
- `routers/` compose `fyers_client` fetches with the engines. `routers/chain.py` is the workhorse (fetch chain → imply forward → solve IV per strike → Greeks). Shared helpers live outside `routers/` (`market_hours`, `chain_pricing`) so nothing imports across the router layer.
- `snapshot_store.py` + `scheduler.py` — at 15:10 IST the cron writes ATM IV for the two expiries either side of 30 days (via `live_iv.py`, the same code `/api/ivrank` uses), and at 15:50 the official close. `atm_iv_history` holds one row **per expiry** per date with a `source` column (`live` / `bhavcopy`). **Never read `iv` straight off it as a series** — go through `get_cm_iv_history`, which interpolates each date to 30 days. IV Rank is the **percentile** of today's 30-day IV in the last 252 dated readings (`get_iv_percentile`), not (IV − low)/(high − low): front-expiry IV is dominated by the weekly roll (6.8 vs 2.2 vol-point range over 29 real sessions), and range-based rank lets one spike pin a year of readings near zero.
- `bhavcopy/` — NSE end-of-day option history. `download.py` is stdlib-only and imports nothing from the backend (NSE blocks datacenter ranges, so it usually runs on a home machine) and stores archive rows raw in `NSE_EOD_DB`; `importer.py` loads per-expiry ATM IV, official closes and per-contract lot sizes into `DB_PATH`, never overwriting. One row per contract per day, no bid/ask, no intraday: it backfills daily research, it does not replace the recorder. **Runbook for downloading, routine updates and adding symbols: `docs/BHAVCOPY.md`.** Downloads must run from a home connection; a date in the last four days with no file yet is retried, never recorded as a holiday. **Pre-2024-07-08 ("legacy") files** are read by column ALIAS (`LEGACY_ALIASES`), because spellings vary by year; a file with option rows for the requested symbols that yields none is logged as an **error** (retried), never as a successful empty day. `--probe YEAR-YEAR` describes one file per year without writing — run it before any long legacy download. American stock options (CA/PA, until about 2011) are stored but never priced. Legacy files publish no underlying price: `bhavcopy/spot.py` estimates it as front future × e^(−r·T), stored in `spot_history` with `source='futures_estimate'`; implied vol does not depend on it, and basis never uses it (circular).
- `alert_engine/` is a self-contained package (own SQLite DB `oi_engine.db`, own models/db/engine modules) running as an asyncio background task, started/stopped via REST endpoints in `routers/alert_engine.py`. State lives in the `engine_state` singleton (`alert_engine/models.py`).
- `market_hours.py` is the single source of truth for session timing, and is **CAS-aware** (see below). Never hardcode 15:30 as the close; use `get_session_phase()` / `is_derivatives_open()`.
- `recorder/` captures full option chains to an **append-only** store (`market_data.db`) for signal research and backtesting. `ChainSnapshot` (`recorder/models.py`) is the contract between live capture and backtest replay — the recorder produces them, `store.iter_snapshots()` replays identical ones. **A signal that imports `fyers_client` is broken by construction.** The recorder auto-starts on successful `/api/auth/validate`. It never deletes: NSE intraday per-strike OI cannot be bought back retroactively.

- `signals/` is the signal framework. `base.py` defines the contract, `registry.py` handles versioned registration, `features.py` derives IV/Greeks/GEX/flip-level/skew from a snapshot, `store.py` logs evaluations, `library.py` holds the registered signals (import it at startup or the registry is empty). **Signals consume `ChainSnapshot` and nothing else** — there is no broker client reachable from a signal, which is what makes live and backtest provably identical. Every evaluation is logged, fired or not: the non-fires are the denominator, and `reason` distinguishes "threshold not met" from "never ran".
- `vrp.py` builds the variance-risk-premium series: 30-day constant-maturity ATM IV (`get_cm_iv_history`) minus 20-session realised vol (`rv_series_from_dated_closes` over `spot_history`), **joined on date, never by position**. The tenors are a pair — 20 sessions is ~28 calendar days, matching the 30-day IV — so changing one without the other breaks the spread. `observations_before()` is the single look-ahead guard: strictly `<`, tested, and every caller goes through it. `load_vrp_history` is shared by the live path and the backtest fixture so the two cannot diverge. **`/api/backtest/run` builds it server-side and passes it as `extras`** — without that the `vrp` signal skips every bar. The same applies to every series-backed signal: **a signal registered but not wired into `backtest/extras.SIGNAL_SERIES` appears in the dropdown and silently never fires**, which reads as "no setups today" rather than as a misconfiguration. A signal module is series-backed if it defines `EXTRAS_KEY`; `test_routers.py` discovers those **from the registry** and fails for any that is not wired in. The VRP pass always runs (its `iv_series` is what vol labelling scores every vol signal against); every other series is built only for the signal that consumes it.
- `term_structure.py` / `skew.py` are the other two ranked daily series (item 33), built on the same rule as `vrp.py`: a pure module plus one loader both the live path and the backtest fixture call. Term structure is 60-day minus 30-day **constant-maturity** IV (`load_term_structure_history`), joined on date — the far leg blanks whenever the far month did not trade, so zipping the two legs would pair today's front with last week's back. `coverage()` reports how often the far leg exists, because a slope series conditioned on the far month having traded is a series of liquid days. Skew is the 25-delta risk reversal, **one reading per session taken from its last bar** (`load_skew_history` → `recorder.store.last_snapshot_of_session`; never walk every minute to keep one). Neither is visible in a `ChainSnapshot` — a snapshot holds one expiry, and the EOD adapter materialises only the front chain — so both arrive through `extras`, and both slice history with `vrp.observations_before`.
- `futures.py` (item 15) — the recorder writes the **front-month future and its expiry** onto every `ChainSnapshot` (`futures`, `futures_expiry`). The front month comes from the exchange's own option calendar (last listed expiry in a month) — **never a weekday rule**; NSE moved monthly expiry Thursday→Tuesday in 2025. Spot and future share one `fetch_quotes` call; a rejected futures symbol drops only itself, and `recorder_state` counts captured vs missed (surfaced on `/api/recorder/status`) because the Fyers symbol format `NSE:NIFTY26OCTFUT` is **not yet verified live**. `load_basis_history` reads the archive's `daily_future` (front contract, traded rows only, `expiry_dt > trad_dt`).
- **Schema changes to `market_data.db` are applied in place** by `recorder.store.init_db` via `_add_missing_columns` — the store is append-only and irreplaceable, so it is never rebuilt. New columns must be nullable; old rows then read as "not captured", which is what they were.
- `cas.py` + `signals/cas_signal.py` (item 34) — closing-auction dislocation: last `CONTINUOUS` bar vs first `POST_CAS` bar, **selected by captured `session_phase`, not the clock**. Two measures, `raw_bps` and `adjusted_bps` (raw minus the future's own move over the window — futures trade through the auction), **never mixed in one percentile**. The official 15:50 close is not used as the close (it does not exist when the signal runs at 15:35); it is joined only as a data check (`print_vs_official_bps`). Dates before `market_hours.CAS_LIVE_DATE` (3 Aug 2026) have no auction. Reads `chain_meta` only, no strike rows.
- `dispersion.py` + `signals/dispersion_signal.py` (item 35) — implied correlation of an index against `config.INDEX_BASKETS`, all at 30-day CM IV. **A proxy**: a few large members, equal-weighted, so ρ's level is biased both ways and can leave [0, 1]; it is **never clipped** and only its percentile is used. The basket is **fixed per series** (a date missing any member is a gap, never a smaller basket) and chosen from data coverage. The backtest scores the index leg only — not a dispersion P&L.
- `backtest/correlation.py` + `POST /api/backtest/correlation` (item 36 prerequisite) — replays signals exactly as the backtester does (`engine.should_carry_history` is shared), one reading per session, and gives each pair a word: `REDUNDANT` / `CO_FIRING` / `DISTINCT` / `INSUFFICIENT_DATA`. On the first real NIFTY run, `dispersion` × `term_structure` came out REDUNDANT (the same stress regime, read in opposite directions) while `vrp` was DISTINCT from both — read this report before combining any of them. No ensemble exists yet, deliberately.
- **Ranked signals compare today with the `lookback` most recent prior readings (default 504, `vrp.DEFAULT_LOOKBACK`), via `vrp.recent_observations`.** The loaders used to keep only 504 dates, which made that the window by accident; `backtest/extras.py` now loads the whole archive (`ALL_HISTORY_DAYS`) so pre-2024 sessions have data, and the ranking window is an explicit parameter so loading more data cannot silently change what a signal compares against.
- `backtest/extras.py` is the ONE place signal history series are assembled (`build_extras(symbol, signal_id, app_db, snapshot_db)`); the router and the CLI both call it, so the Research page and a command-line run cannot hand a signal different histories. `snapshot_db` points the skew/CAS series at the bars actually being replayed.
- `backtest/cli.py` — `python -m backtest.cli run|correlation` backtests over the bhavcopy history, which **the Research page cannot reach** (it replays `market_data.db`). It materialises missing archive dates into `eod_snapshots.db` (one bar per session) on every run, so downloading new days and re-running is enough. Runbook: `docs/BHAVCOPY.md` → "Backtesting signals on this history".
- `bhavcopy/snapshots.py` rebuilds exchange EOD rows as `ChainSnapshot`s so daily-horizon signals replay through the same backtester. Tagged `SessionPhase.END_OF_DAY` — one bar per contract per day, **no bid/ask**, so `price_for_iv` falls back to the close and a spread-aware cost model correctly refuses the fill. `materialize()` writes to a **separate database** on purpose: mixing one-per-day bars into `market_data.db` would make a replay interleave them with the recorder's one-per-minute bars. Pass that file to `run_backtest(db_path=...)`. EOD bars are scored on the daily horizons automatically.
- `backtest/` replays recorded snapshots through the registry. The history window is appended to only *after* evaluation, so look-ahead is structurally impossible rather than merely discouraged. **Every result is reported against a matched null** (random entries at the same clock times, same direction mix) — a signal is not alpha until it beats that, and the verdict strings (`NO_EDGE`, `INSUFFICIENT_DATA`) are deliberately words rather than numbers you can talk yourself into. **In daily and vol modes the verdict comes from a circular-shift test (`backtest/significance.py`), never from the Welch t.** Signals ranked on slow series fire in runs, and consecutive multi-session outcomes overlap, so a t-test counting them as independent said EDGE on 35.5% of simulated no-edge worlds; the shift test (the signal's own firing pattern slid to every non-overlapping offset) says 7%. Results are also split **by direction** (`BacktestRun.by_direction`), each against its own baseline, because a both-ways signal's aggregate can hide a losing side. **Intraday verdicts still use the Welch t and still overstate significance** — the run carries a note saying so. Horizons in `labels.py` are fixed in advance; never tune them per signal. **Two labelling modes**: intraday (`5m/15m/30m/60m/eod`, null matched on clock time) and daily (`1d/5d/20d` counting trading sessions, null matched on weekday to control for the weekly expiry cycle). **A third mode, `vol`**, scores SHORT_VOL / LONG_VOL by (IV at entry minus subsequently realised vol) in VOL POINTS — signed underlying return cannot evaluate a volatility position, which pays on vol whichever way the index moves. `run_backtest` picks the mode: an explicit `label_mode` argument, else the horizon the signal **declared at registration** (`register_signal(..., label_mode="daily")` — for a hypothesis whose horizon the bars cannot reveal, e.g. an overnight claim evaluated on minute bars), else vol-family directions, else one bar per session means daily. `BacktestRun.unit` is `bps` or `vol_points`; the two are never comparable. Vol mode needs an entry-IV series via `extras={'iv_series': ...}` (or `vrp_series`). `label_mode` forces any mode. **History windows carry across sessions for one-bar-per-session data and deliberately do not for intraday**, where yesterday's last minutes would let an OI-velocity rule match a spike across the overnight gap; `run_backtest` decides from `session_snapshot_counts`. **Testing a VRP-style signal: IV sits on both sides** — in the input (IV minus trailing RV) and the outcome (IV minus future RV) — so edge can appear because wide premiums mean-revert rather than because the signal times anything.

- `trading/` is the trade lifecycle: `models.py` (state machine — illegal transitions raise), `sizing.py`, `entry.py`, `exits.py`, `broker.py`, `portfolio.py`, `manager.py` (the only module that mutates trade state), `store.py`. **Everything is paper by default and no endpoint places a live order** — `LiveBroker` deliberately raises `NotImplementedError`. Two rules worth keeping: sizing **floors to whole lots** (rounding 0.8 up to 1 silently takes ~25% more risk than budgeted), and **shorts are sized off stop distance, never premium received**. Paper fills reuse `backtest/costs.py` on purpose — if paper were more optimistic than the backtester, forward results would diverge from backtest results for reasons unrelated to the signal.
- `notify/` is the alert layer: `base.py` (channel interface + `Dispatcher` with dedupe and severity floor), `telegram.py`, `events.py` (message builders). Trade code imports `notify`, never a specific channel. **Channel failures are caught and logged, never propagated** — a missed Telegram message is an inconvenience; an exception reaching position management is not. Telegram needs `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the backend environment; without them the channel reports unconfigured rather than raising.

### Frontend layering

- `context/AppContext.jsx` holds only global state: token/validity, symbol, expiry, spot, symbol list. Everything else is local page state — no Redux.
- `hooks/` (`useSurface`, `useOI`, `useChain`, `useIVRank`, `useExpiries`) own all data fetching and expose `{ data, loading, error, refetch }`. Pages (`pages/`) compose hooks + components; they do not fetch directly.
- `App.jsx` gates the whole dashboard behind `TokenGate` until the token validates.
- Plotly (`react-plotly.js`) renders the 3D surface, skew, term structure, and payoff diagrams; Recharts renders the OI/GEX bar charts.

## Conventions and gotchas

- **Fyers expiry dates are DD-MM-YYYY** (e.g. `"24-04-2025"`); all date parsing uses `"%d-%m-%Y"`. Do not assume ISO format on anything coming from Fyers.
- **Anything derived from a snapshot measures time from `snapshot.observed_at()`, never the wall clock.** `compute_features` used to call `time_to_expiry(expiry)` with no `now`, so on every historical bar the option had "already expired", T was 0 and every IV-derived feature was `None` — `skew_rr25` could never fire on history and `gex_regime` replays broke a week after recording. Tests must build expiries relative to the snapshot's date, not today; a today-relative fixture is what hid this.
- **Time to expiry comes from `market_hours.time_to_expiry()`** — fractional, measured to the real 15:30 IST expiry instant, IST-aware. Never reintroduce whole-day `(expiry - today).days`: that returns 0.0 across all of expiry day, which silently disables IV and Greeks everywhere, and `date.today()` is the wrong calendar day on a UTC host. `days_to_expiry` is a deprecated alias.
- **The IV solver returns `None` when IV is not identifiable, and that is deliberate.** Newton-Raphson falls back to bracketed bisection, so convergence is not the constraint; the constraint is information. Deep ITM and far OTM prices are flat in vol, so a solver that always converges returns a confident wrong answer (7DTE deep ITM was solving 26.25% against a true 16%). `MIN_VEGA` in `iv_engine.py` gates on the quote's own resolution: if a one-vol-point move shifts the model price by less than half a 0.05 tick, there is no IV in the data. Downstream code must handle `None` — never substitute a default.
- Option types are the NSE strings `"CE"` / `"PE"` throughout, not call/put booleans.
- Unit conventions: engines work in decimals (IV 0.14), the API boundary returns percentages (14.0) — routers do the ×100. `greeks()` returns Vega/Rho per 1% move and Theta per calendar day; `bs_vega()` is per-unit (used by the solver).
- Adding an underlying means adding one entry to `UNDERLYINGS` in `config.py` — symbol key, Fyers symbol string, fallback lot size, strike step.
- **Read lot sizes through `lot_sizes.lot_size_for(symbol, expiry)`, never `UNDERLYINGS[...]["lot_size"]`.** NSE revises them several times a year and during a revision near and far expiries carry different sizes; the config values had drifted to NIFTY 75 / BANKNIFTY 15 against an exchange 65 / 30, which made the sizer take twice the intended risk on BANKNIFTY. The lookup uses per-contract sizes from the bhavcopy import and falls back to config.
- **ATM IV has one definition: `chain_pricing.atm_iv_for_chain`** (mean of solvable call and put IV at the strike nearest the implied forward). Live endpoint, daily job and importer all use it; IV Rank compares their outputs, so a second definition shows up as a fake regime shift.
- **Exchange EOD data traps** (measured on real files, handled in `bhavcopy/`): an untraded contract's published close is exactly its previous close, so only traded rows are priced; on expiry day `SttlmPric` is the underlying's settlement level, not a premium; `OpnIntrst` is in **shares** while `TtlTradgVol` is in **contracts**; `TtlTrfVal` is notional on the underlying, not premium turnover.
- The Fyers app client ID is hardcoded in `fyers_client.py` (`FYERS_CLIENT_ID`); only the access token is user-supplied.
- `POST /api/position-lab/calculate` is the only unauthenticated data endpoint — pure BS math, no Fyers call.
- Frontend styling: light "Golden Hour" cream palette (`#FBF7F0` bg, teal `#0D9488` / red `#DC2626` accents, IBM Plex Mono) with a Plotly `LAYOUT_BASE` duplicated at the top of each page — there is no central theme file; copy from an existing page. (The README's "dark terminal" description is outdated.)

## CAS — Closing Auction Session (live 3 Aug 2026)

SEBI/NSE replaced VWAP closing-price discovery with a call auction for
F&O-eligible **cash stocks**. Continuous cash trading now ends **15:15**, the
auction runs to ~15:35, and **derivatives trade until ~15:40**. Index options
aren't directly in CAS, but the official index close derives from constituent
closes that now come from CAS. Consequences encoded in `market_hours.py`:

- Anything polling the option chain must gate on `is_derivatives_open()` (to 15:40),
  not a 15:30 cutoff. `alert_engine.is_market_open()` now delegates here.
- The IV snapshot runs at 15:10, before the auction, and the official close is
  captured separately at 15:50 from the exchange daily candle. Never capture a
  closing price between 15:15 and the auction's end: for the stock underlyings
  it is a stale pre-auction print. Guard with `is_cash_price_reliable()`.
- Every recorded snapshot is tagged with `SessionPhase`; an unchanged LTP during
  `CAS_WINDOW` is a frozen book, not a quiet market.

## Known quirks / tech debt

- `IV_SOLVER_*` constants in `config.py` are never imported; the real defaults live in `iv_engine.py` (`IV_LOWER_BOUND`, `IV_UPPER_BOUND`, `MIN_VEGA`, `BISECTION_MAX_ITER`).
- In-memory-only state lost on restart: the snapshot token (`scheduler.py`), the alert-engine task handle, and the recorder task handle. A restart mid-session stops recording until the token is validated again.
- Alert-engine comments reference `app_v2_final.py` (the original Streamlit app it was ported from) — that file is not in the repo.
- README drift: it describes 3 modules (the Alert Engine page/package, SVI interpolation, realized vol, max pain, PCR are missing), says "36 unit tests" (now 256), uses `cd optionslens/backend` paths (actual: `backend/` at repo root), and references a `fyers_login_test.py` that isn't committed.
- `realized_vol.compute_rv_series` is the deprecated undated version — use `rv_series_from_dated_closes` with `spot_history`.
- `start_scheduler()` is guarded against a second call: starting twice would bind jobs to a loop the first caller already closed. Matters for test harnesses and reloads, not production.
- GEX multiplies OI by lot size. Exchange files report OI in shares; if Fyers does too, GEX is overstated by the lot size (a scale factor only — the flip level is unaffected). Not yet verified against a live Fyers response.
- Monthly-only contracts can have an untraded second month, which leaves no 30-day reading from bhavcopy history on those dates. Measured over 2024-07 to 2026-09: FINNIFTY had almost none, while the five configured stocks had 89-100% of dates covered (ICICIBANK lowest). The importer's "CM30 days" column reports it per symbol.
