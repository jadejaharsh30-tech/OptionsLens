# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read this first

**`docs/ROADMAP.md` is the project's durable memory.** It holds the 55-item
phased checklist (signal research → backtesting → trade lifecycle → notifications),
the guiding principles, the CAS regulatory findings, and a progress log. Read it
at the start of every session and tick items off as they land.

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
```

Tests cover only the pure-Python engines (`iv_engine`, `gex_engine`, `svi_engine`, `realized_vol`, `snapshot_store`, `alert_engine/percentile_threshold`) and need no Fyers token or network. Routers and `fyers_client` are untested — exercising them requires a live token.

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
- `/api/auth/validate` (main.py) doubles as token registration: it stores the token in-memory in `scheduler.py` so the daily 15:20 IST snapshot cron job can use it. If no token was validated that day, the snapshot silently skips.

### Backend layering

- `config.py` — single source of truth: `UNDERLYINGS` (Fyers symbol strings, lot sizes, strike steps), `RISK_FREE_RATE`, IV solver params, `DB_PATH`.
- Pure-math engines (`iv_engine.py`, `gex_engine.py`, `svi_engine.py`, `realized_vol.py`) take plain floats/lists and know nothing about Fyers or FastAPI. Keep them dependency-free — this is what makes them unit-testable.
- `routers/` compose `fyers_client` fetches with the engines. `routers/chain.py` is the workhorse (fetch chain → solve IV per strike → Greeks); other routers reuse its helpers (e.g. `days_to_expiry`).
- `snapshot_store.py` + `scheduler.py` — APScheduler cron writes daily ATM IV + spot to SQLite at 15:20 IST; this self-built history powers `/api/ivrank` (no broker provides historical IV).
- `alert_engine/` is a self-contained package (own SQLite DB `oi_engine.db`, own models/db/engine modules) running as an asyncio background task, started/stopped via REST endpoints in `routers/alert_engine.py`. State lives in the `engine_state` singleton (`alert_engine/models.py`).
- `market_hours.py` is the single source of truth for session timing, and is **CAS-aware** (see below). Never hardcode 15:30 as the close; use `get_session_phase()` / `is_derivatives_open()`.
- `recorder/` captures full option chains to an **append-only** store (`market_data.db`) for signal research and backtesting. `ChainSnapshot` (`recorder/models.py`) is the contract between live capture and backtest replay — the recorder produces them, `store.iter_snapshots()` replays identical ones. **A signal that imports `fyers_client` is broken by construction.** The recorder auto-starts on successful `/api/auth/validate`. It never deletes: NSE intraday per-strike OI cannot be bought back retroactively.

### Frontend layering

- `context/AppContext.jsx` holds only global state: token/validity, symbol, expiry, spot, symbol list. Everything else is local page state — no Redux.
- `hooks/` (`useSurface`, `useOI`, `useChain`, `useIVRank`, `useExpiries`) own all data fetching and expose `{ data, loading, error, refetch }`. Pages (`pages/`) compose hooks + components; they do not fetch directly.
- `App.jsx` gates the whole dashboard behind `TokenGate` until the token validates.
- Plotly (`react-plotly.js`) renders the 3D surface, skew, term structure, and payoff diagrams; Recharts renders the OI/GEX bar charts.

## Conventions and gotchas

- **Fyers expiry dates are DD-MM-YYYY** (e.g. `"24-04-2025"`); all date parsing uses `"%d-%m-%Y"`. Do not assume ISO format on anything coming from Fyers.
- **Time to expiry comes from `market_hours.time_to_expiry()`** — fractional, measured to the real 15:30 IST expiry instant, IST-aware. Never reintroduce whole-day `(expiry - today).days`: that returns 0.0 across all of expiry day, which silently disables IV and Greeks everywhere, and `date.today()` is the wrong calendar day on a UTC host. `days_to_expiry` is a deprecated alias.
- IV solver returns `None` for illiquid strikes (Vega → 0) rather than raising; downstream code must handle `None` IVs.
- Option types are the NSE strings `"CE"` / `"PE"` throughout, not call/put booleans.
- Unit conventions: engines work in decimals (IV 0.14), the API boundary returns percentages (14.0) — routers do the ×100. `greeks()` returns Vega/Rho per 1% move and Theta per calendar day; `bs_vega()` is per-unit (used by the solver).
- Adding an underlying means adding one entry to `UNDERLYINGS` in `config.py` — symbol key, Fyers symbol string, lot size, strike step.
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
  not the old 15:30 cutoff — `alert_engine.is_market_open()` is still stale here.
- **`SNAPSHOT_TIME_IST = "15:20"` now fires inside the auction window**, so the
  "closing spot" it captures for the 5 stock underlyings is a stale pre-auction
  print, silently contaminating `spot_history` → realized vol → any VRP signal.
  Guard closing-price capture with `is_cash_price_reliable()`.
- Every recorded snapshot is tagged with `SessionPhase`; an unchanged LTP during
  `CAS_WINDOW` is a frozen book, not a quiet market.

## Known quirks / tech debt

- **`DB_PATH` env var is dead**: `docker-compose.yml` sets `DB_PATH` and mounts `./data`, but `config.py` hardcodes `DB_PATH = "optionslens.db"` and no backend code reads env vars — SQLite data does not actually persist across Docker rebuilds. Fixing persistence means making `config.py` read the env var.
- `IV_SOLVER_*` constants in `config.py` are never imported; the real defaults are duplicated in `implied_volatility()`'s signature.
- `routers/oi.py` solves IV from bid/ask mid; `chain.py`/`surface.py`/`ivrank.py` use raw LTP — the same strike can report slightly different IVs across endpoints. Mid is the better input (LTP goes stale on illiquid strikes); unifying this is roadmap item 6.
- In-memory-only state lost on restart: the snapshot token (`scheduler.py`) and the alert-engine asyncio task handle. A restart before 15:20 IST silently skips that day's IV snapshot.
- `fetch_historical_prices` (fyers_client.py) has a dead, broken epoch computation immediately overwritten by the correct one.
- Alert-engine comments reference `app_v2_final.py` (the original Streamlit app it was ported from) — that file is not in the repo.
- README drift: it describes 3 modules (the Alert Engine page/package, SVI interpolation, realized vol, max pain, PCR are missing), says "36 unit tests" (now 52), uses `cd optionslens/backend` paths (actual: `backend/` at repo root), and references a `fyers_login_test.py` that isn't committed.
