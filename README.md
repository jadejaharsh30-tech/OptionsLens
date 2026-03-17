# OptionsLens

NSE options market intelligence dashboard — built as a Quant Dev portfolio project.

## What it does

### Module 1 — Market Structure
- **3D IV surface** across all expiries (strike × DTE × IV) — rotatable, colour-coded teal→amber→red
- **IV skew curves** per expiry — call IV vs put IV, with ATM reference line
- **Term structure** — ATM IV plotted across all expiry dates
- **IV Rank gauge** — current IV as a percentile of the past year (builds from day 1 of running)

### Module 2 — Smart Money
- **OI by strike** — butterfly bar chart, calls vs puts, identifies key S/R levels
- **OI change** — buildup vs unwinding vs session open, colour-coded by direction
- **Gamma Exposure (GEX)** — net dealer gamma by strike; positive = pin risk, negative = vol expansion zone
- Auto-refreshes every 60s during market hours (09:15–15:30 IST)

### Module 3 — Position Lab
- **Multi-leg strategy builder** — add/remove legs, select live strikes from chain
- **IV auto-fill** — selecting a strike pre-fills the current market IV
- **Net Greeks** — Delta, Gamma, Vega, Theta, Rho for the full position
- **Payoff diagram** — P&L at expiry + P&L today overlaid, with breakeven markers
- **Scenario engine** — slide spot ±20%, IV ±50%, time forward up to 30 days

## Architecture

```
FastAPI backend
├── Fyers API v3        — live NSE options chain (auth via daily access token)
├── IV engine           — Newton-Raphson IV solver on Black-Scholes (pure Python)
├── Greeks engine       — Delta, Gamma, Vega, Theta, Rho from closed-form BS
├── GEX engine          — Gamma × OI × lot_size × spot² × 0.01 per strike
├── Snapshot store      — SQLite; daily 15:20 IST cron builds IV Rank history
└── APScheduler         — async cron job for daily snapshot

React frontend
├── Plotly.js           — 3D surface, skew curves, term structure, payoff diagram
├── Recharts            — OI butterfly, OI change, GEX bars
├── React Context       — token, symbol, expiry shared globally
├── Axios + Vite proxy  — zero hardcoded URLs; /api/* forwarded to backend in dev
└── Tailwind CSS v3     — dark terminal-finance aesthetic (JetBrains Mono + DM Sans)
```

## Tech stack

| Layer    | Tech                                      |
|----------|-------------------------------------------|
| Backend  | Python 3.11, FastAPI, APScheduler, SQLite |
| Frontend | React 18, Vite, Plotly.js, Recharts, Tailwind CSS v3 |
| Data     | Fyers API v3 (live), local SQLite (IV history) |

## Run locally

```bash
# 1. Backend
cd optionslens/backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# 2. Frontend
cd optionslens/frontend
npm install
npm run dev

# 3. Open http://localhost:5173
#    Paste your Fyers access token when prompted (run fyers_login_test.py to generate)
```

## Run with Docker

```bash
cd optionslens
docker compose up --build
# Frontend: http://localhost:80
# Backend API docs: http://localhost:8000/docs
```

## Key technical decisions

**IV solver from scratch** — Fyers does not return IV. The backend implements
Newton-Raphson iteration: `σ_new = σ_old − (BS(σ_old) − market_price) / Vega(σ_old)`.
Converges in ~5 iterations. Gracefully returns `None` for illiquid strikes where
Vega → 0. 36 unit tests cover BS pricing, put-call parity, roundtrip IV recovery,
edge cases.

**Self-built IV history** — No broker provides historical options chain snapshots.
A daily APScheduler job at 15:20 IST snapshots ATM IV for all underlyings to SQLite.
IV Rank becomes meaningful after ~5 days and is fully reliable after 30 days.

**GEX model** — Assumes dealers are short options, delta-hedging creates gamma
exposure `GEX = Γ × OI × lot_size × S² × 0.01`. Positive net GEX = dealers long
gamma = stabilising (price pins at high-GEX strikes). Negative = short gamma =
dealers amplify moves (trend days, vol expansions).

**No Redux** — AppContext holds only token + symbol + expiry. All page-level state
is local. Hooks (`useSurface`, `useOI`, `useChain`, `useIVRank`) own data fetching
and expose `{ data, loading, error, refetch }` — pages compose, not fetch.
