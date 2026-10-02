# OptionsLens — Methodology

How this system turns NSE option prices into volatility measurements, trading
signals and verdicts about those signals, and why each step is built the way it
is. Every quantitative claim below was measured on this codebase or its data;
where something is an assumption rather than a measurement, it says so.

All quantitative code (Black-76, implied volatility, Greeks, GEX, SVI,
realised volatility, the statistics) is written from scratch in pure Python,
with no numerical or quant libraries, so every number can be traced to a
line of code.

---

## The governing principle

**Return nothing rather than a confident wrong number.**

Most of the decisions below are instances of it. The IV solver returns `None`
where a price carries no information about volatility. A constant-maturity
series leaves a gap rather than extrapolate past what the data supports. A
backtest says `INSUFFICIENT_DATA` rather than report a statistic from too few
observations. A downloader logs a year it cannot parse as an error rather than
as a successful day with no options. A downstream consumer can handle an
honest gap. It cannot detect a plausible-looking fabrication.

---

## 1. Pricing: Black-76 on an implied forward, never on spot

**What.** Every implied volatility is solved with Black-76 against a forward
recovered from the option chain's own put-call parity, using the VIX
convention of the strike where |C − P| is smallest
(`forward_engine.py`, `chain_pricing.implied_forward_for_chain`).

**Why not spot.** Pricing an index option off spot with Black-Scholes means
assuming a carry (risk-free rate minus dividend yield) that the market may
not share. The error is not random. It inflates call IVs and deflates put IVs
by roughly the dividend yield. On NIFTY that was **about one vol point,
varying by strike** — which reads as skew on a chart and breaks put-call
parity in the system's own numbers. Recovering the forward from parity
makes call and put IV at the same strike agree by construction, because the
market's own carry is inside the forward.

**Spot's remaining role.** Only a sanity bound: a recovered forward more than
25% from spot is rejected (`MAX_FORWARD_DEVIATION`). That is why an
*estimated* spot is acceptable on historical dates (section 6).

**One definition of ATM IV.** `chain_pricing.atm_iv_for_chain` (mean of the
solvable call and put IV at the strike nearest the forward) is used by the
live endpoint, the daily job and the history importer. IV Rank compares
readings from all three. A second definition anywhere would show up as a fake
regime shift.

## 2. When implied volatility does not exist

**What.** The solver (Newton-Raphson with a bracketed bisection fallback and a
Brenner-Subrahmanyam initial guess) returns `None` when a quote cannot
identify a volatility (`iv_engine.py`).

**Why.** Convergence is not the hard part; bisection always converges. The hard
part is *information*. Deep in- and out-of-the-money option prices barely move
when volatility changes, so many volatilities fit the same price. A solver
that always returns a number returns a confident wrong one. Measured: a
7-day deep-in-the-money contract solved to **26.25% against a true 16%**.

**The gate.** `MIN_VEGA = (tick / 2) / 0.01 = 2.5`. If moving volatility one
point changes the model price by less than half a tick (0.05), the quote's own
resolution cannot distinguish volatilities that far apart, so there is no IV
in the data. The threshold comes from the instrument, not from tuning.

## 3. Time

**Fractional time to expiry.** T is measured to the actual expiry instant
(15:30 IST), not in whole days (`market_hours.time_to_expiry`). The whole-day
version returned exactly zero all through expiry day, which silently disabled
IV and Greeks across the chain on the day 0-DTE gamma matters most. It also
used the server's calendar date, which on a UTC host is the wrong Indian day
every evening.

**Time is measured from the observation, not from now.** Anything derived
from a recorded snapshot measures T from that snapshot's own timestamp
(`ChainSnapshot.observed_at()`). Measuring from the wall clock made every
historical option look expired. T was zero, and every IV-derived feature on
every historical bar came back `None`. That bug was invisible in tests whose
fixtures dated their expiries relative to *today*: two errors cancelling.

## 4. A volatility series that is not mostly calendar

**Constant maturity.** Front-expiry ATM IV is dominated by the weekly roll as
the contract decays toward expiry. Measured over 29 sessions of exchange
data: the front-expiry series swung **6.80 vol points** peak to trough; a
30-day constant-maturity series built from the same chains swung **2.20**.
The other 4.6 points were the calendar.

Every series that is ranked or compared therefore interpolates to a fixed
tenor between the two bracketing expiries, **in total variance** (σ²T), as
VIX does, because variance, not volatility, is additive in time
(`eod_vol.constant_maturity_iv`). It extrapolates at most five days past the
nearest expiry (needed for monthly-only stocks just after an expiry) and
otherwise leaves a gap.

**Percentile, not range.** IV Rank is the percentile of today's reading among
the previous 252, not (IV − low) / (high − low). With the range version, one
crisis day sets the range for a year and pins every later reading near zero.

**An explicit ranking window.** Ranked signals compare today with the 504
prior readings (about two years). For a while this was only a side effect of
a loader that kept 504 dates. When the loaders were changed to return the
whole archive, the window had to become an explicit parameter. Otherwise
loading more data would have silently turned a two-year rank into an
eighteen-year one: a different signal.

## 5. Signals: one code path, live and replayed

**Signals see a `ChainSnapshot` and nothing else.** The recorder produces
snapshots live; the backtester replays identical ones from disk. No broker
client is reachable from signal code, so a signal cannot behave differently
in a backtest than live.

**Daily history arrives through one builder.** Signals that rank against
years of daily readings (the variance risk premium, the term structure,
implied correlation, skew, the closing-auction gap) receive that history
through `backtest/extras.py`. It is the only place those series are built, so
the dashboard and a command-line run cannot hand a signal different
histories. A registered signal that is not wired into that builder appears in
the dropdown and then never fires. A test discovers every such signal from
the registry and fails if any is unwired.

**Every evaluation is logged**, fired or not, with the reason for a non-fire.
The non-fires are the denominator of every hit rate. "Threshold not met" and
"never ran" are different kinds of silence.

**Look-ahead is structurally impossible.** The replay appends a bar to the
signal's history only *after* evaluating it. History series are sliced by a
single tested function that is strictly `<` the current date
(`vrp.observations_before`). A `<=` there would rank each observation against
a set containing itself, and nothing in the output would show it.

## 6. Data: exchange end-of-day history

NSE's daily derivatives files (2000 onwards) supply the history. Traps
measured on real files, and how they are handled (`bhavcopy/`):

- An **untraded contract's published close is its previous close.** Only
  traded rows are priced; otherwise a stale price becomes a fake IV.
- On **expiry day the settlement column holds the index level**, not an
  option price, so expiring contracts are excluded on that day.
- **Open interest is in shares; volume is in contracts.**
- **Before 8 July 2024** the files use an older format: column spellings vary
  by year, and **no underlying price is published.** Columns are read by
  alias. A file that has option rows for the requested symbols but yields none
  is logged as an *error*, not as a successful empty day, so a misread year
  cannot silently disappear. A probe mode describes one file per year before
  any bulk download. It ran against real files from every year 2001-2024.
- **Stock options were American-style until 2011** (published as CA/PA).
  They are stored but never priced with a European model; stock IV history
  starts in 2011.
- **Spot for pre-2024 dates is estimated** from the front-month future as
  S ≈ F·e^(−rT), ignoring the dividend yield. The error is at most about
  0.15% for a ≤35-day contract. That is irrelevant to IV, since spot is only
  a 25% sanity bound there, and negligible for realised volatility, where a
  slowly drifting level bias cancels out of daily returns. It is *circular*
  for basis, so basis never uses it. Every estimated close is stored with
  `source='futures_estimate'`.

## 7. The closing auction (CAS)

Since 3 August 2026, F&O-eligible cash stocks stop continuous trading at 15:15
and close through a call auction ending around 15:35; derivatives trade until
about 15:40. Index closes are built from those auction prints. Consequences:

- Every recorded snapshot carries a session phase (`CONTINUOUS`, `CAS_WINDOW`,
  `POST_CAS`, …). An unchanged price during the auction is a frozen book, not
  a quiet market.
- The daily IV snapshot runs at 15:10, before the auction. The official close
  is captured separately afterwards. A closing price sampled during the
  auction would be a stale pre-auction print.
- The auction creates a new measurable quantity: the gap between the last
  continuous price and the auction print (`cas.py`). Futures keep trading
  while the cash book is frozen, so subtracting the future's own move over the
  same window separates "the market moved" from "the auction printed away from
  where derivatives priced it." The two measures are never mixed in one
  percentile.

## 8. Testing a signal: what counts as evidence

**A matched null.** Every result is compared with random entries carrying the
signal's direction mix, matched on clock time for intraday horizons and on
weekday for daily ones, because the weekly expiry falls on a fixed weekday.
A signal that fires mostly on expiry days is compared with other expiry days.

**Horizons fixed in advance**, never tuned per signal: intraday 5, 15, 30 and
60 minutes plus end of day; daily 1, 5 and 20 sessions.

**The right outcome for the claim.** A directional signal is scored on the
underlying's return in basis points. A volatility signal is scored on implied
volatility at entry minus volatility realised afterwards, in vol points,
because a short-volatility position pays when realised volatility comes in
under implied, whichever way the index moves. The units are never mixed.
That labeller was validated in both directions: a fixture where rich premiums
genuinely preceded calm scored +12.43 vol points (t = 18.85), and a fixture
with no such relationship correctly returned no edge.

**Verdicts are words** (`EDGE`, `NO_EDGE`, `INSUFFICIENT_DATA`), not a
statistic the eye can talk itself into.

**Costs.** Fills are spread-aware (buy at the ask, sell at the bid) and carry
STT, exchange, SEBI, stamp duty and GST; a one-sided book cannot be filled.
Paper trading reuses the backtester's cost model, so forward and backtest
results cannot diverge for reasons unrelated to the signal.

## 9. Significance when observations are not independent

This is the section where the system's own first results turned out to be
wrong.

**The problem.** Signals ranked on slow-moving series (a volatility premium,
an implied correlation) fire in long runs of consecutive sessions, and a
20-session outcome started one day shares 19 of its 20 days with the next
day's. Two hundred fires can carry about a dozen independent observations.
The first verdicts used a Welch t-test that counted all two hundred.

**Measured, not argued.** On 1,000 simulated markets where the signal had
**no edge by construction** (549 sessions, persistent percentile signal,
overlapping 20-session outcomes), the t-test said `EDGE` in **35.5%** of them,
against the 5% it claims. The first real runs on NIFTY had reported two
`EDGE` verdicts through it.

**Alternatives, also measured on the same simulation:**

| Test | False `EDGE` rate (target 5%) | Verdict |
|---|---|---|
| Welch t, observations treated as independent | 35.5% | rejected |
| Newey-West standard errors, lag 20-40 | ~14% | rejected: the persistent regressor defeats its small-sample correction |
| Means of non-overlapping blocks | 0% | rejected: no power at all |
| **Circular shift** | **7%** | **adopted**; detects a half-standard-deviation edge 54% of the time |

**The circular-shift test** keeps the outcome series exactly as it happened
and slides the signal's own firing pattern (its runs, gaps and direction mix)
to every offset long enough that no shifted outcome overlaps an original one.
The p-value is the share of placements that did at least as well as the real
one. The null is therefore "this exact pattern of bets, placed at a random
time", which keeps both things the t-test discards.

**Intraday** horizons never cross a session, so the overlap lives inside one.
Sliding fires along the minute axis would break the clock-time matching the
intraday null exists for. The intraday test instead moves each *session's*
whole firing pattern, at the same clock times, onto every other session, with
outcomes centred on the mean for their clock time. On simulated sessions with
no edge, a morning drift and a signal that fires more in the morning, the
t-test said `EDGE` **37%** (15 minutes) and **60%** (end of day) of the time.
The session shift said **5.3%** and **6.7%**, and detected a real 1 bp-per-fire
edge 85% of the time over 40 sessions. It needs at least 20 sessions to return
any verdict, because 19 offsets is the fewest that can reach p = 0.05.

**Results are split by direction.** A signal that fires both ways can hide a
losing side inside a winning total. Each direction is tested against what
*that* direction earned on an average session.

**What the shift tests still assume:** that the outcome series is stationary
over the sample. A signal that fires mostly in one half of a sample whose
halves differ can still look significant. Two years of one market is one
regime history; no test fixes that.

## 10. What the evidence says so far

First runs on NIFTY, 549 exchange sessions (July 2024 to September 2026),
default parameters, the 20-session horizon named in advance as the headline:

| Signal | Edge (vol points) | p (circular shift) | Verdict |
|---|---|---|---|
| Variance risk premium | +0.89 | 0.19 | `NO_EDGE` |
| Implied correlation (dispersion) | +1.29 | 0.18 | `NO_EDGE` |
| Term structure | −0.35 | 0.83 | `NO_EDGE` |

The two earlier `EDGE` verdicts were the t-test. Read as **low power, not zero
edge**: two years of one market only resolves large effects. If the variance
risk premium's +0.89 were real and constant, roughly 2.3 times the data would
reach p = 0.05.

Two things the data said that the design had assumed otherwise:

- The term-structure signal assumed an index volatility curve is "normally in
  contango". For NIFTY's 30-to-60-day segment it is **flat on average**:
  negative on 53.6% of dates, with the distribution centred near zero. The
  premise was corrected in the code's comments. The parameter deliberately
  was not, since changing it after seeing the result would be fitting.
- Three volatility signals were expected to share one calm/stress driver.
  Measured co-firing showed implied correlation and term structure are one
  observation read in opposite directions, while the variance risk premium is
  **distinct from both**. An ensemble counting them as three confirmations
  would have counted one regime three times. No ensemble exists yet,
  deliberately.

**The out-of-sample test, with its rule committed before it ran** (the
pre-registered protocol in `docs/ROADMAP.md`). The 2024-2026 period had been
examined, so the signals were frozen as they stood and run on NIFTY's unseen
history: 5,710 sessions, June 2001 to July 2024 (implied correlation from 2011,
when stock options became European). A signal had to reach p ≤ 0.017 (0.05
across three signals) at the 20-session horizon, with the in-sample sign.

| Signal | In-sample edge | Out-of-sample edge | p | Survives |
|---|---|---|---|---|
| Variance risk premium | +0.89 | +0.19 | 0.803 | no |
| Term structure | −0.35 | +0.52 | 0.306 | no, and the sign flipped |
| Implied correlation | +1.29 | +0.47 | 0.878 | no |

**None survives.** One cell outside the headline reached p = 0.032 (term
structure, 10 sessions). It is above the bar, it is not the horizon named in
advance, and with nine signal-horizon cells one such p-value is about what
chance produces. It is recorded as not a finding.

**What the history did show** is that the premium itself is real. On an
average session, a short-volatility position earned about **+1.8 vol points**
over the following 20 sessions (implied above what was subsequently
realised), and a long one lost the same. The three signals did not time that
premium measurably better than holding it every day. A strategy built on the
unconditional premium is a new hypothesis on data that has now been seen, so
it gets its own pre-registration and is judged only on data not yet used: the
forward record the daily runner now keeps.

## 11. Engineering choices that protect the numbers

- **Market data is never deleted.** Intraday per-strike open interest cannot
  be bought back later. Schema changes to the recorder's store are applied in
  place to a store that is never rebuilt.
- **Raw fields are recorded and everything is derived late,** so fixing the
  pricing model retroactively fixes all of history instead of invalidating it.
- **Readers never create the database they were asked to read.** A missing
  file is a missing file, not a silently created empty one.
- **CI runs the full test suite on Linux and Windows.** The system is
  developed on one and run on the other, and three bugs so far appeared only
  on Windows or only for a non-administrator account.
- **Live execution does not exist.** Every trade is paper. The live broker
  deliberately raises; wiring real orders is a separate, explicit decision.
  Position sizing floors to whole lots, because rounding 0.8 of a lot up to
  one takes 25% more risk than budgeted, and short options are sized off
  their stop distance, never off the premium received.
