# optionslens/backend/tests/test_bhavcopy.py
"""
bhavcopy importer end to end, on a synthetic archive priced with Black-76 at a
known vol, so every assertion has an exact answer.

The traps it pins are the ones measured on real exchange files: untraded
contracts carry the previous close as their "close", and expiry-day settlement
is the underlying's level rather than an option price.
"""
import math
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bhavcopy import download                                    # noqa: E402
from bhavcopy.importer import import_history                     # noqa: E402
from config import RISK_FREE_RATE                                # noqa: E402
from iv_engine import black76_price                              # noqa: E402
from lot_sizes import lot_size_for                               # noqa: E402
from market_hours import time_to_expiry                          # noqa: E402
from snapshot_store import (get_atm_iv_history, get_cm_iv_history,  # noqa: E402
                            get_spot_history, write_atm_iv)

VOL = 0.15
SPOT = 24_000.0
DAYS = [date(2026, 8, 3) + timedelta(days=i) for i in range(5)]     # Mon-Fri


def _row(day, symbol, expiry, strike, ot, close, volume, underlying, lot):
    return {c: None for c in download.OPTION_COLS} | {
        "trad_dt": day.isoformat(), "symbol": symbol, "expiry_dt": expiry.isoformat(),
        "strike": strike, "option_type": ot, "close": close, "settle": close,
        "volume": volume, "underlying": underlying, "lot_size": lot,
        "instrument": "IDO", "era": "udiff", "oi": 1000.0,
    }


def _price(day, expiry, strike, ot, vol=VOL):
    T = time_to_expiry(expiry.strftime("%d-%m-%Y"),
                       now=_close(day))
    F = SPOT * math.exp(RISK_FREE_RATE * T)
    return round(black76_price(F, strike, T, RISK_FREE_RATE, vol, ot), 2)


def _close(day):
    from datetime import datetime
    from market_hours import EXPIRY_TIME_IST, IST
    return datetime.combine(day, EXPIRY_TIME_IST, tzinfo=IST)


@pytest.fixture
def dbs(tmp_path):
    src = tmp_path / "eod.db"
    app = tmp_path / "app.db"
    conn = download.connect(src)
    rows = []
    for day in DAYS:
        near = day + timedelta(days=16)
        far = day + timedelta(days=44)
        for expiry, lot in ((near, 65), (far, 75)):          # a lot-size revision
            for k in range(23_500, 24_550, 50):
                for ot in ("CE", "PE"):
                    rows.append(_row(day, "NIFTY", expiry, float(k), ot,
                                     _price(day, expiry, k, ot), 500.0, SPOT, lot))
        # Untraded ATM contract in a third expiry with a stale, wrong close.
        stale = day + timedelta(days=72)
        for ot in ("CE", "PE"):
            rows.append(_row(day, "NIFTY", stale, 24_000.0, ot, 999.0, 0.0, SPOT, 75))
        # Expiring contract: settlement is the index level, not a premium.
        rows.append(_row(day, "NIFTY", day, 24_000.0, "CE", 12.0, 90_000.0, SPOT, 65)
                    | {"settle": SPOT})
        # A symbol the app does not track.
        rows.append(_row(day, "FINNIFTY", near, 23_000.0, "CE", 100.0, 500.0, 23_000.0, 60))
    download.insert(conn, "daily_option", download.OPTION_COLS, rows)
    conn.commit()
    conn.close()
    return str(src), str(app)


def test_recovers_the_known_vol_at_thirty_days(dbs):
    src, app = dbs
    import_history(src, app)
    cm = get_cm_iv_history(app, "NIFTY")
    assert len(cm) == len(DAYS)
    for row in cm:
        assert row["iv"] == pytest.approx(VOL, abs=5e-4)


def test_untraded_contracts_never_reach_the_store(dbs):
    src, app = dbs
    import_history(src, app)
    stale = (DAYS[0] + timedelta(days=72)).strftime("%d-%m-%Y")
    rows = get_atm_iv_history(app, "NIFTY")
    assert all(r["expiry_date"] != stale for r in rows)
    assert len({r["expiry_date"] for r in rows if r["date"] == DAYS[0].isoformat()}) == 2


def test_expiring_contracts_are_skipped(dbs):
    src, app = dbs
    import_history(src, app)
    for r in get_atm_iv_history(app, "NIFTY"):
        assert r["expiry_date"] != date.fromisoformat(r["date"]).strftime("%d-%m-%Y")


def test_writes_the_underlying_close(dbs):
    src, app = dbs
    import_history(src, app)
    spots = get_spot_history(app, "NIFTY")
    assert len(spots) == len(DAYS)
    assert all(s["spot"] == SPOT for s in spots)


def test_untracked_symbols_are_left_out_by_default(dbs):
    src, app = dbs
    stats = import_history(src, app)
    assert set(stats) == {"NIFTY"}
    assert get_spot_history(app, "FINNIFTY") == []


def test_explicit_symbols_are_honoured(dbs):
    src, app = dbs
    stats = import_history(src, app, symbols={"FINNIFTY"})
    assert set(stats) == {"FINNIFTY"}


def test_rerun_adds_nothing(dbs):
    src, app = dbs
    first = import_history(src, app)
    second = import_history(src, app)
    assert first["NIFTY"].iv_rows_added > 0
    assert second["NIFTY"].iv_rows_added == 0
    assert second["NIFTY"].spot_rows_added == 0


def test_live_rows_are_not_overwritten(dbs):
    src, app = dbs
    from snapshot_store import init_db
    init_db(app)
    near = (DAYS[0] + timedelta(days=16)).strftime("%d-%m-%Y")
    write_atm_iv(app, DAYS[0].isoformat(), "NIFTY", near, 0.42)
    import_history(src, app)
    conn = sqlite3.connect(app)
    iv, source = conn.execute(
        "SELECT iv, source FROM atm_iv_history WHERE snapshot_date=? AND expiry_date=?",
        (DAYS[0].isoformat(), near)).fetchone()
    conn.close()
    assert (iv, source) == (pytest.approx(0.42), "live")


def test_lot_sizes_are_recorded_per_expiry(dbs):
    src, app = dbs
    import_history(src, app)
    near = DAYS[0] + timedelta(days=16)
    far = DAYS[0] + timedelta(days=44)
    assert lot_size_for("NIFTY", near.strftime("%d-%m-%Y"), db_path=app) == 65
    assert lot_size_for("NIFTY", far.isoformat(), db_path=app) == 75


def test_source_database_is_opened_read_only(dbs):
    src, app = dbs
    before = Path(src).stat().st_mtime_ns
    import_history(src, app)
    assert Path(src).stat().st_mtime_ns == before


def test_dates_without_an_underlying_price_are_counted_not_guessed(tmp_path):
    src, app = tmp_path / "eod.db", tmp_path / "app.db"
    conn = download.connect(src)
    day, expiry = date(2024, 6, 27), date(2024, 7, 25)
    download.insert(conn, "daily_option", download.OPTION_COLS, [
        _row(day, "NIFTY", expiry, 23_500.0, ot, 100.0, 500.0, None, None)
        | {"era": "legacy"} for ot in ("CE", "PE")])
    conn.commit(); conn.close()
    stats = import_history(str(src), str(app))
    assert stats["NIFTY"].dates_without_spot == 1
    assert get_atm_iv_history(str(app), "NIFTY") == []
