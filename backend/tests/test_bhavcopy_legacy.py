"""
Pre-2024-07-08 ("legacy") history tests.

These files could not be inspected from the environment the code was written
in (NSE blocks datacenter connections), so the tests pin BEHAVIOUR under the
format variations that are known or plausible, and above all pin that an
unrecognised format fails LOUDLY:

  - a misspelt option-type column is still found (2003/2005/2006 were observed
    to differ);
  - a file whose option rows cannot be parsed is logged as an ERROR and
    retried, never recorded as a successful day with zero options — the old
    behaviour, which would have left years silently empty;
  - American-style stock options (CA/PA, until 2011) are stored but never
    priced with a European model;
  - the underlying price, absent from these files, is estimated from the
    front future, tagged as an estimate, and identical in the importer and the
    snapshot adapter;
  - --probe describes a year's file without writing anything.
"""
import csv
import io
import math
import os
import sqlite3
import sys
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bhavcopy import download as dl  # noqa: E402
from config import RISK_FREE_RATE  # noqa: E402
from iv_engine import black76_price  # noqa: E402

HEADERS = ["INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP", "OPEN",
           "HIGH", "LOW", "CLOSE", "SETTLE_PR", "CONTRACTS", "VAL_INLAKH",
           "OPEN_INT", "CHG_IN_OI", "TIMESTAMP", ""]      # real files end in a comma


def legacy_zip(day: date, fut: float = 5100.0, iv: float = 0.25,
               headers=HEADERS, opt_types=("CE", "PE"), symbol="NIFTY",
               stock_rows=False, drop_timestamp=False) -> bytes:
    """A legacy-format bhavcopy: one monthly option chain plus two futures."""
    expiry = day + timedelta(days=24)
    far = day + timedelta(days=52)
    T = 24 / 365
    stamp = day.strftime("%d-%b-%Y").upper()
    rows = []

    def put(values: dict):
        rows.append([values.get(h, "") for h in headers])

    col = dict(zip(["INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP",
                    "CLOSE", "CONTRACTS", "OPEN_INT", "CHG_IN_OI", "TIMESTAMP"],
                   headers[:5] + ["CLOSE", "CONTRACTS", "OPEN_INT", "CHG_IN_OI",
                                  "TIMESTAMP"]))
    for k in range(-4, 5):
        strike = round(fut / 50) * 50 + k * 50
        for published, model in zip(opt_types, ("CE", "PE")):
            px = black76_price(fut, strike, T, RISK_FREE_RATE, iv, model)
            put({col["INSTRUMENT"]: "OPTIDX", col["SYMBOL"]: symbol,
                 col["EXPIRY_DT"]: expiry.strftime("%d-%b-%Y"),
                 col["STRIKE_PR"]: f"{strike:.2f}", col["OPTION_TYP"]: published,
                 "CLOSE": f"{px:.2f}", "CONTRACTS": "500", "OPEN_INT": "10000",
                 "CHG_IN_OI": "100",
                 "TIMESTAMP": "" if drop_timestamp else stamp})
    for exp, px in ((expiry, fut), (far, fut * 1.004)):
        put({col["INSTRUMENT"]: "FUTIDX", col["SYMBOL"]: symbol,
             col["EXPIRY_DT"]: exp.strftime("%d-%b-%Y"), col["STRIKE_PR"]: "0",
             col["OPTION_TYP"]: "XX", "CLOSE": f"{px:.2f}", "CONTRACTS": "9000",
             "OPEN_INT": "1", "CHG_IN_OI": "0",
             "TIMESTAMP": "" if drop_timestamp else stamp})
    if stock_rows:
        for t in ("CA", "PA"):
            put({col["INSTRUMENT"]: "OPTSTK", col["SYMBOL"]: "INFOSYSTCH",
                 col["EXPIRY_DT"]: expiry.strftime("%d-%b-%Y"),
                 col["STRIKE_PR"]: "2500.00", col["OPTION_TYP"]: t, "CLOSE": "80",
                 "CONTRACTS": "50", "OPEN_INT": "1", "CHG_IN_OI": "0",
                 "TIMESTAMP": stamp})

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr(f"fo{day:%d%b%Y}bhav.csv".upper(), buf.getvalue())
    return out.getvalue()


def ingest(monkeypatch, db: str, payloads: dict[date, bytes], symbols=("NIFTY",),
           stocks=False) -> dict:
    monkeypatch.setattr(dl, "fetch", lambda url: next(
        (p for d, p in payloads.items() if dl.url_for(d) == url), None))
    conn = dl.connect(Path(db))
    out = {d: dl.ingest_day(conn, d, set(symbols), stocks) for d in payloads}
    conn.close()
    return out


D = date(2010, 3, 15)


# ── Column resolution ─────────────────────────────────────────────────────────

def test_standard_legacy_headers_resolve():
    cols, missing = dl.legacy_columns(HEADERS)
    assert missing == []
    assert cols["option_type"] == "OPTION_TYP" and cols["volume"] == "CONTRACTS"


@pytest.mark.parametrize("spelling", ["OPTIONTYPE", "OPT_TYPE", "Option_Typ",
                                      "OPTN_TYPE", "OPTIONTYPES"])
def test_a_misspelt_option_type_column_is_still_found(spelling):
    cols, missing = dl.legacy_columns([spelling if h == "OPTION_TYP" else h
                                       for h in HEADERS])
    assert missing == []
    assert cols["option_type"] == spelling


def test_a_misspelt_year_still_ingests_its_options(monkeypatch):
    headers = ["OPTIONTYPE" if h == "OPTION_TYP" else h for h in HEADERS]
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "a.db")
        [(status, n_opt, n_fut)] = ingest(
            monkeypatch, db, {D: legacy_zip(D, headers=headers)}).values()
    assert status == "ok" and n_opt == 18 and n_fut == 2


# ── Failing loudly ────────────────────────────────────────────────────────────

def test_unparseable_option_types_are_an_error_not_a_silent_zero(monkeypatch):
    """
    The old behaviour: a day whose options could not be read was logged "ok"
    with zero options, marked done, and never retried.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "a.db")
        [(status, n_opt, _)] = ingest(
            monkeypatch, db, {D: legacy_zip(D, opt_types=("C", "P"))}).values()
        conn = sqlite3.connect(db)
        note, = conn.execute("SELECT note FROM ingest_log").fetchone()
        done = dl.already_done(conn, D)
        conn.close()
    assert status == "error" and n_opt == 0
    assert not done                                   # retried on the next run
    assert "none parsed" in note and "'C'" in note    # names what it saw


def test_missing_required_columns_are_an_error_naming_the_headers(monkeypatch):
    headers = ["STRIKE" if h == "STRIKE_PR" else h for h in HEADERS]
    headers = ["STRK" if h == "STRIKE" else h for h in headers]   # unknowable
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "a.db")
        [(status, _, _)] = ingest(monkeypatch, db,
                                  {D: legacy_zip(D, headers=headers)}).values()
        conn = sqlite3.connect(db)
        note, = conn.execute("SELECT note FROM ingest_log").fetchone()
        conn.close()
    assert status == "error"
    assert "strike" in note and "STRK" in note


def test_a_day_before_options_existed_is_not_an_error(monkeypatch):
    """A file with futures only (no option rows at all) is a success."""
    buf = io.StringIO()
    csv.writer(buf).writerows([HEADERS, ["FUTIDX", "NIFTY", "25-Jan-2001", "0", "XX",
                                         "", "", "", "1500", "", "900", "", "1", "0",
                                         "04-JAN-2001", ""]])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("x.csv", buf.getvalue())
    with tempfile.TemporaryDirectory() as tmp:
        [(status, n_opt, n_fut)] = ingest(
            monkeypatch, os.path.join(tmp, "a.db"),
            {date(2001, 1, 4): out.getvalue()}).values()
    assert (status, n_opt, n_fut) == ("ok", 0, 1)


# ── Content quirks ────────────────────────────────────────────────────────────

def test_american_stock_options_are_stored_under_the_current_symbol(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "a.db")
        ingest(monkeypatch, db, {D: legacy_zip(D, stock_rows=True)},
               symbols=("NIFTY", "INFY"), stocks=True)
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT symbol, option_type FROM daily_option "
                            "WHERE symbol != 'NIFTY' ORDER BY option_type").fetchall()
        conn.close()
    assert rows == [("INFY", "CA"), ("INFY", "PA")]


def test_legacy_dates_parse_in_every_seen_form():
    assert dl.iso_date("04-JAN-2010", "legacy") == "2010-01-04"
    assert dl.iso_date("04-Jan-2010", "legacy") == "2010-01-04"
    assert dl.iso_date("04-Jan-10", "legacy") == "2010-01-04"
    assert dl.iso_date("", "legacy") is None


def test_a_file_without_a_timestamp_column_uses_the_date_it_was_fetched_for(monkeypatch):
    headers = [h for h in HEADERS if h != "TIMESTAMP"]
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "a.db")
        [(status, n_opt, _)] = ingest(monkeypatch, db,
                                      {D: legacy_zip(D, headers=headers)}).values()
        conn = sqlite3.connect(db)
        dates = {r[0] for r in conn.execute("SELECT trad_dt FROM daily_option")}
        conn.close()
    assert status == "ok" and n_opt == 18 and dates == {D.isoformat()}


# ── Probe ─────────────────────────────────────────────────────────────────────

def test_probe_describes_a_year_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = date(2010, 1, 11)                         # 10 Jan 2010 is a Sunday
    info = dl.probe_year(2010, {"NIFTY", "INFY"}, include_stocks=True, pause=0,
                         fetcher=lambda url: legacy_zip(first, stock_rows=True)
                         if url == dl.url_for(first) else None)
    assert info["status"] == "OK"
    assert info["kept_by_symbol"] == {"NIFTY": 18, "INFY": 2}
    assert info["option_types"]["CA"] == 1
    assert info["underlying_published"] is False
    assert list(tmp_path.iterdir()) == []            # no database created


def test_probe_flags_a_year_it_cannot_read():
    first = date(2005, 1, 10)
    info = dl.probe_year(2005, {"NIFTY"}, include_stocks=False, pause=0,
                         fetcher=lambda url: legacy_zip(first, opt_types=("C", "P")))
    assert info["status"] == "PROBLEM"
    assert "none parsed" in info["note"]


def test_probe_reports_a_year_with_no_files():
    info = dl.probe_year(1999, None, False, pause=0, fetcher=lambda url: None)
    assert info["status"] == "NO_FILE"


# ── Spot estimate ─────────────────────────────────────────────────────────────

def test_spot_estimate_discounts_the_future_by_the_risk_free_rate():
    from bhavcopy.spot import estimate_spot
    from market_hours import EXPIRY_TIME_IST, IST, time_to_expiry

    est = estimate_spot(5100.0, "2010-04-08", "2010-03-15", 0.065)
    T = time_to_expiry("08-04-2010", now=datetime.combine(
        date(2010, 3, 15), EXPIRY_TIME_IST, tzinfo=IST))
    assert abs(est - 5100.0 * math.exp(-0.065 * T)) < 1e-9
    assert est < 5100.0


def test_a_published_price_always_beats_the_estimate():
    from bhavcopy.spot import SOURCE_PUBLISHED, spot_for_date
    conn = sqlite3.connect(":memory:")
    assert spot_for_date(conn, "NIFTY", "2025-01-06", 23500.0) == (23500.0, SOURCE_PUBLISHED)


def test_no_price_and_no_future_is_no_spot():
    from bhavcopy.download import SCHEMA
    from bhavcopy.spot import spot_for_date
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    assert spot_for_date(conn, "NIFTY", "2010-03-15", None) == (None, None)


# ── Importer and snapshots, end to end ────────────────────────────────────────

def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_legacy_history_imports_with_estimated_spot_and_matches_the_snapshots(monkeypatch):
    from bhavcopy.importer import import_history
    from bhavcopy.snapshots import iter_eod_snapshots
    from snapshot_store import get_spot_history

    days = weekdays(D, 5)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "nse.db")
        app = os.path.join(tmp, "app.db")
        ingest(monkeypatch, src,
               {d: legacy_zip(d, fut=5100 + 10 * i, stock_rows=True)
                for i, d in enumerate(days)}, symbols=("NIFTY", "INFY"), stocks=True)

        stats = import_history(src, app, symbols={"NIFTY", "INFY"})
        nifty = stats["NIFTY"]
        spots = {r["date"]: r["spot"] for r in get_spot_history(app, "NIFTY", 10)}
        conn = sqlite3.connect(app)
        sources = {r[0] for r in conn.execute(
            "SELECT source FROM spot_history WHERE symbol='NIFTY'")}
        iv_n, = conn.execute(
            "SELECT COUNT(*) FROM atm_iv_history WHERE symbol='INFY'").fetchone()
        conn.close()
        snap = next(iter_eod_snapshots("NIFTY", days[0].isoformat(), src, 25))

    assert nifty.dates_without_spot == 0
    assert nifty.dates_spot_estimated == 5
    assert nifty.expiries_solved >= 5                       # IV solved on legacy dates
    assert sources == {"futures_estimate"}
    assert iv_n == 0                                        # CA/PA never priced
    # The snapshot adapter and spot_history agree to the paisa.
    assert abs(snap.spot - spots[days[0].isoformat()]) < 1e-6
    assert snap.spot < snap.futures


def test_spot_history_gains_a_source_column_in_place():
    from snapshot_store import get_spot_history, init_db, write_spot_many

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "old.db")
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE spot_history (id INTEGER PRIMARY KEY
            AUTOINCREMENT, snapshot_date TEXT NOT NULL, symbol TEXT NOT NULL,
            spot REAL NOT NULL, UNIQUE(snapshot_date, symbol))""")
        conn.execute("INSERT INTO spot_history (snapshot_date, symbol, spot) "
                     "VALUES ('2025-01-06', 'NIFTY', 23500)")
        conn.commit()
        conn.close()

        init_db(db)
        init_db(db)                                         # idempotent
        write_spot_many(db, [("2010-03-15", "NIFTY", 5090.0)], "futures_estimate")
        conn = sqlite3.connect(db)
        rows = dict(conn.execute("SELECT snapshot_date, source FROM spot_history"))
        conn.close()
        assert len(get_spot_history(db, "NIFTY", 10)) == 2
    assert rows == {"2025-01-06": None, "2010-03-15": "futures_estimate"}


def test_lines_with_surplus_fields_no_longer_crash_the_day(monkeypatch):
    """
    Real 2002-2003 files carry lines with more fields than the header; the csv
    module files the surplus as a list under a None key, and every such day
    failed with "'list' object has no attribute 'strip'". Empty surplus (stray
    commas) is harmless; a line with real surplus values is skipped and counted.
    """
    payload = legacy_zip(D)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        name = zf.namelist()[0]
        text = zf.read(name).decode()
    lines = text.splitlines()
    lines.insert(2, lines[1] + ",,")                       # stray trailing commas
    lines.append("OPTIDX,NIFTY,08-Apr-2010,5000.00,CE,,,,1,,5,,1,0,15-MAR-2010,,EXTRA")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr(name, "\n".join(lines) + "\n")

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "a.db")
        [(status, n_opt, n_fut)] = ingest(monkeypatch, db, {D: out.getvalue()}).values()
        conn = sqlite3.connect(db)
        note, = conn.execute("SELECT note FROM ingest_log").fetchone()
        conn.close()
    assert status == "ok"
    assert n_opt == 18 and n_fut == 2       # the duplicated line is INSERT OR IGNORE'd
    assert note == "1 malformed line(s) skipped"


def test_a_second_import_processes_only_new_dates(monkeypatch):
    """
    With 25 years in the archive, re-solving every date on every routine
    update made each update as slow as the first import.
    """
    from bhavcopy.importer import import_history

    days = weekdays(D, 6)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "nse.db")
        app = os.path.join(tmp, "app.db")
        ingest(monkeypatch, src, {d: legacy_zip(d) for d in days[:5]})
        first = import_history(src, app, symbols={"NIFTY"})["NIFTY"]
        ingest(monkeypatch, src, {days[5]: legacy_zip(days[5])})
        second = import_history(src, app, symbols={"NIFTY"})["NIFTY"]
        full = import_history(src, app, symbols={"NIFTY"}, full=True)["NIFTY"]

    assert (first.dates, first.dates_already) == (5, 0)
    assert (second.dates, second.dates_already) == (1, 5)
    assert second.iv_rows_added > 0
    assert (full.dates, full.iv_rows_added) == (6, 0)   # redone, nothing duplicated
