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
```

Check `update_history.log` now and then for lines saying ERROR.

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

## Not done yet

- **History before 8 July 2024.** Files go back to 2000, but two fixes are
  needed first. Some years spell the option-type column differently, which the
  downloader does not read yet. Older files publish no underlying price, so spot
  must be rebuilt from the futures rows the downloader already stores. Monthly
  signals such as VRP and term structure could usefully start around 2008.
  Weekly-expiry signals only have history from 2016 (BANKNIFTY) and 2019 (NIFTY).
- **Replaying daily history through the backtester.** This needs an adapter
  from these rows to `ChainSnapshot`, and a new end-of-day value in
  `SessionPhase`.
- **Realized volatility still uses Fyers closes**, with approximate dates.
  `spot_history` now holds exact exchange closes that could replace them.
