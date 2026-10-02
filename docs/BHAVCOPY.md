# NSE end-of-day history: keeping it current

How to download option history from NSE and load it into OptionsLens, once and
then on a routine. The code lives in `backend/bhavcopy/`. Why it exists and
what the data can and cannot do is in `docs/ROADMAP.md`, item 30 and the
2026-09-23 progress entry.

## The two steps

1. **Download** (`python -m bhavcopy.download`) fetches NSE's daily derivatives
   file for each trading date and stores the rows raw in `nse_options_eod.db`.
   It uses only the Python standard library.
2. **Import** (`python -m bhavcopy.importer`) reads that file and adds per-expiry
   ATM IV, official closes and per-contract lot sizes to the app's database
   (`optionslens.db`). It never overwrites a row, so running it again is always
   safe.

## Where to run it

**On a home connection.** NSE refuses most datacenter, cloud and VPN addresses,
and often corporate networks. If every date prints "no file (holiday)", the
network is being refused. The dates are not really holidays.

**Keep one master copy of `nse_options_eod.db`.** Put it in the `backend`
folder: both commands look there by default, so no paths are needed. It is
gitignored and is never committed. The file grows by roughly 1 MB per trading
day for the symbols below.

## Routine update

From `backend/`, with the virtual environment active:

```
python -m bhavcopy.download --from 2024-07-08 --to today --stocks --symbols NIFTY,BANKNIFTY,RELIANCE,TCS,HDFCBANK,INFY,ICICIBANK
python -m bhavcopy.importer
```

The command is the same every time:

- **Dates already downloaded are skipped** without contacting NSE, so only new
  days are fetched.
- **`today` means the current date in India.**
- **Run it in the evening.** NSE publishes each day's file after the close. A
  date from the last four days whose file is not published yet prints "not
  published yet" and is retried on the next run. It is never recorded as a
  holiday. Older missing dates are genuine holidays.

How often: weekly is enough if the app runs most trading days, because its
15:10 job writes the same readings live. The import fills whatever days the app
missed. Where a live reading and an imported one exist for the same date and
expiry, the live one is kept.

### With Docker

Keep the master file in the project's `data` folder, which is shared with the
container. Run the download on your machine and the import inside the container:

```
cd backend
python -m bhavcopy.download --from 2024-07-08 --to today --stocks --symbols NIFTY,BANKNIFTY,RELIANCE,TCS,HDFCBANK,INFY,ICICIBANK --db ..\data\nse_options_eod.db
docker compose exec backend python -m bhavcopy.importer
```

### Automating it on Windows

Save this as `update_history.bat` somewhere outside the repository, with your
own project path, and schedule it with Task Scheduler for about 21:00 on
weekdays:

```
@echo off
cd /d C:\path\to\OptionsLens\backend
call .venv\Scripts\activate
python -m bhavcopy.download --from 2024-07-08 --to today --stocks --symbols NIFTY,BANKNIFTY,RELIANCE,TCS,HDFCBANK,INFY,ICICIBANK >> update_history.log 2>&1
python -m bhavcopy.importer >> update_history.log 2>&1
python -m daily_signals --source eod --notify >> update_history.log 2>&1
```

Check `update_history.log` now and then for lines saying ERROR. The last line
runs the daily signals on the newest session and sends the digest; see the
next section.

## Daily signals on the newest session

After the update, evaluate every daily signal on the newest session in the
archive:

```
python -m daily_signals --source eod
```

It prints a digest in three parts. **FIRED** lists the signals that fired,
with direction and strength. **QUIET** lists the signals that had today's
reading but found nothing extreme. **UNAVAILABLE** lists the signals with no
reading to judge, and why. Every evaluation is also written to the evaluation
log in `market_data.db`. A rerun keeps the first evaluation of a session.

- `--date 2026-10-01` evaluates a past session instead of the newest.
- `--symbols NIFTY,BANKNIFTY` limits the run.
- `--verbose` adds what history each symbol had, which is the first thing to
  read when a signal says UNAVAILABLE.
- `--notify` sends the digest to Telegram, once per session. `--force` sends it
  again.

Every fire is labelled `[unvalidated]` until that signal passes the
out-of-sample test. **These are hypotheses under test, not trade advice.**

**`--source eod` versus the app.** The app runs the same signals by itself at
16:05 IST on the recorder's own bars (`--source recorder`). The two sources
cover different signals. `skew_rr25` needs an end-of-day bar, so it runs only
on `eod`. `cas_dislocation` reads the closing auction off intraday bars, so it
runs only on `recorder`. The term structure needs a traded expiry beyond 60
days, which only the bhavcopy import supplies.

### Telegram, once

1. In Telegram, message **@BotFather**, send `/newbot`, and follow the prompts.
   Copy the token it gives you.
2. Send your new bot any message. Then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the
   number after `"chat":{"id":`.
3. Store both for your Windows user, in cmd:

   ```
   setx TELEGRAM_BOT_TOKEN "123456:ABC-your-token"
   setx TELEGRAM_CHAT_ID "123456789"
   ```

   `setx` applies to windows opened **afterwards**, so open a new cmd window
   before the next step. The values stay on your machine and are never
   committed.
4. Test it with `python -m daily_signals --source eod --notify --force`.

A digest that cannot be sent prints the reason, for example that no channel is
configured. The session is not marked as sent, so the next run tries again.

## Checking the data

```
python -m bhavcopy.download --report
```

This prints rows per symbol, the date range, and the ingest log, which counts
dates as `ok`, `no_file` or `error`. Re-running the update retries `error`
dates. To list the dates recorded as holidays:

```
python -c "import sqlite3; c=sqlite3.connect('nse_options_eod.db'); print(*[r[0] for r in c.execute('SELECT trad_dt FROM ingest_log WHERE status=? ORDER BY trad_dt', ('no_file',))], sep='\n')"
```

## Adding a symbol later

Dates are marked done per date, not per symbol. Adding a symbol to `--symbols`
therefore changes nothing for dates already downloaded, unless you add `--force`:

```
python -m bhavcopy.download --from 2024-07-08 --to today --stocks --force --symbols NIFTY,BANKNIFTY,RELIANCE,TCS,HDFCBANK,INFY,ICICIBANK,NEWSYMBOL
python -m bhavcopy.importer
```

`--force` downloads the whole range again, taking roughly 30 to 40 minutes.
Rows already stored are kept, and only the new symbol's rows are added. The app
itself only shows symbols listed in `UNDERLYINGS` in `config.py`, and the
importer only loads those unless given `--symbols`.

## Backtesting signals on this history

The Research page replays the **live recorder's** store, which only holds the
sessions recorded since the recorder started. To run a signal over this
archive's two years instead, use the command line from `backend/`:

```
python -m backtest.cli run --signal vrp --symbol NIFTY
python -m backtest.cli run --signal term_structure --symbol NIFTY
python -m backtest.cli run --signal dispersion --symbol NIFTY
python -m backtest.cli correlation --symbol NIFTY --signals vrp,term_structure,dispersion
```

The first run rebuilds the archive as one bar per session into
`eod_snapshots.db` (a separate file on purpose: mixing daily bars into
`market_data.db` would interleave them with the recorder's minute bars). Later
runs only add dates you have downloaded since, so the routine update above
followed by the same command is enough. Signals receive exactly the history the
Research page would give them.

Override a parameter with `--param`, repeatable:

```
python -m backtest.cli run --signal skew_rr25 --symbol NIFTY --param mode=momentum
python -m backtest.cli run --signal vrp --symbol NIFTY --param rich_percentile=90
```

Reading the result: `p` is from a circular-shift test that allows for the
signal firing on runs of consecutive days and for overlapping multi-day
outcomes; `EDGE` needs p ≤ 0.05. The "By direction" table tests buy-vol and
sell-vol (or long and short) fires separately against what that direction
earned on an average day. Read it whenever a signal fires both ways: a losing
side can hide inside a winning total.

**Every parameter you try is another test.** The verdict does not know how
many you ran. Decide the parameters before looking, and treat a result found
by sweeping as a hypothesis for data you have not used yet, not as a finding.

What will and will not work here:

- `vrp`, `term_structure`, `dispersion`: yes. Their history is the daily IV and
  closes the importer loaded. `dispersion` is for indices only, and uses
  whichever members were imported (it says which).
- `skew_rr25`: yes, but thin. It needs both 25-delta wings traded on the front
  expiry, which exchange files often do not have.
- `gex_regime`, `oi_short_buildup`: they run, but read them with care. They
  were designed for intraday bars with bid and ask; here they see one closing
  bar per day with neither.
- `cas_dislocation`: no. It needs the pre-auction and post-auction minutes,
  which only the live recorder captures.

## Facts about the data worth remembering

- One row per contract per trading day. There is no bid, no ask and nothing
  intraday. Intraday research still depends on the live recorder.
- An untraded contract's published close is just its previous close. Only
  traded contracts are priced, and the importer ignores the rest.
- On expiry day, the settlement column holds the index level, not an option
  price.
- Open interest is in shares, and volume is in contracts.
- Measured on 2024-07-08 to 2026-09-10: every one of the 30 dates with no file
  was an exchange holiday, and the configured stocks had a 30-day IV reading on
  89-100% of dates.

## Downloading history before July 2024 (run at home)

Files before 8 July 2024 use an older format, with no underlying price and
column spellings that vary by year. The run starts with a PROBE that shows how
each year parses before anything is downloaded in bulk. First probe
(2026-09-30): every year 2002-2024 parsed; options exist from 2002 (NIFTY,
INFY, RELIANCE), 2004 (HDFCBANK, ICICIBANK), 2005 (TCS) and 2006
(BANKNIFTY); stock options are American (CA/PA, not priced) until 2011.
NSE blocks datacenter connections, not every network: the probe worked from
an office network as well as from home.
All commands are for `cmd.exe`, from `backend` with the venv active.

**1. Probe: about a minute, writes nothing.**

```
python -m bhavcopy.download --probe 2001-2024 --stocks --symbols NIFTY,BANKNIFTY,RELIANCE,TCS,HDFCBANK,INFY,ICICIBANK
```

One line block per year. `OK` means the year parses. `PROBLEM` prints the
file's headers and what failed to read. **If any year says PROBLEM, stop and
send the output** — it is exactly what is needed to fix the parser. Also worth
reading: "requested but absent" lists symbols the year's file does not carry
(stocks entered F&O at different times; INFY is believed to appear as
INFOSYSTCH before 2011, which is mapped automatically).

**2. Download.** Roughly 4,300 trading days, 1.5 to 3 hours:

```
python -m bhavcopy.download --from 2008-01-01 --to 2024-07-05 --stocks --symbols NIFTY,BANKNIFTY,RELIANCE,TCS,HDFCBANK,INFY,ICICIBANK
```

Safe to stop with Ctrl+C and re-run the same command; finished days are
skipped. A day whose options could not be parsed is logged as an error and
retried on the next run — it is never recorded as a day with no options.
Earlier years can be added later the same way (`--from 2001-01-01 --to
2007-12-31`).

**3. Check, then import.**

```
python -m bhavcopy.download --report
python -m bhavcopy.importer
```

The importer's `est spot` column counts dates whose underlying price was
estimated from the front-month future, since these files publish none. The
estimate is stored as `futures_estimate`, never mixed silently with published
closes; implied vol does not depend on it (see `bhavcopy/spot.py` for the
error bound).

**4. The out-of-sample test.** Replay only the years the signals have never
been tested on, with the parameters UNCHANGED:

```
python -m backtest.cli run --signal vrp --symbol NIFTY --to 2024-07-05
python -m backtest.cli run --signal term_structure --symbol NIFTY --to 2024-07-05
python -m backtest.cli run --signal dispersion --symbol NIFTY --to 2024-07-05
```

The first run rebuilds several thousand sessions into `eod_snapshots.db` and
takes a few minutes. Signals still rank each day against the ~504 readings
before it (`lookback`), so a 2012 reading is compared with 2010-2012, exactly
as today's is compared with the last two years.

What the older data can and cannot give:

- Stock options were American-style until 2011 (published as CA/PA). They are
  stored but not priced, so stock IV history starts around 2011.
  `dispersion` therefore has no basket before then.
- NIFTY options had only monthly expiries before 2019. The 30-day reading
  interpolates between two monthlies; far months were thin in early years, so
  expect gaps (the importer's `CM30 days` column shows how many).

## Not done yet

- **Years before 2008** are supported by the same code; only the date range
  above stops at 2008. Early far-month liquidity is the likely limit.
