# optionslens/backend/bhavcopy/__init__.py
"""
Exchange end-of-day option data, downloaded from NSE's public bhavcopy archive.

Two halves, deliberately separate:

  download  — stdlib-only fetcher that stores the archive's option and futures
              rows raw in their own SQLite file. Runs anywhere; imports nothing
              from the backend.
  importer  — reads that file and loads derived history into the app's own
              stores: per-expiry ATM IV, official closes, and per-expiry lot
              sizes.

What this data is and is not: one row per contract per trading day. It has no
bid or ask and no intraday timestamps, so it cannot stand in for the recorder.
It exists to give daily-horizon research (IV history, VRP, term structure)
years of history instead of the weeks the recorder has accumulated.
"""
