"""
Futures capture tests (roadmap item 15).

What would fail silently, and so is pinned here:

  - **Picking the wrong contract.** The front month is read from the exchange's
    own option calendar, never from a weekday rule — NSE moved monthly expiry
    from Thursday to Tuesday in 2025, and a hardcoded rule written before that
    would still "work" while quoting the wrong month.
  - **A rejected futures symbol costing the spot quote.** Futures are best
    effort; spot is not. One bad symbol in a batched quote must drop only itself.
  - **Old databases.** `market_data.db` is append-only and irreplaceable, so the
    new `futures_expiry` column is added in place, and rows recorded before it
    existed must read back as "not captured", not break the replay.
  - **Basis from an untraded contract.** Its published close is yesterday's, so
    the "basis" would be a day's return mislabelled as carry.
"""
import math
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from futures import (  # noqa: E402
    front_month_expiry, fyers_future_symbol, implied_carry, load_basis_history,
    monthly_expiries, summarise_basis,
)
from market_hours import IST, SessionPhase  # noqa: E402
from recorder.models import ChainRow, ChainSnapshot  # noqa: E402

SYM = "NIFTY"


def at(y, m, d, hh=10, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


# A realistic index listing: Tuesday weeklies with the month's last Tuesday as
# the monthly, then quarterly far months.
INDEX_EXPIRIES = [
    "06-10-2026", "13-10-2026", "20-10-2026", "27-10-2026",
    "03-11-2026", "24-11-2026", "29-12-2026", "30-03-2027",
]


# ── Which contract ────────────────────────────────────────────────────────────

def test_monthly_expiries_keep_the_last_expiry_of_each_month():
    days = [datetime.strptime(s, "%d-%m-%Y").date() for s in INDEX_EXPIRIES]
    assert monthly_expiries(days) == [
        date(2026, 10, 27), date(2026, 11, 24), date(2026, 12, 29), date(2027, 3, 30),
    ]


def test_front_month_is_the_nearest_monthly_not_the_nearest_weekly():
    assert front_month_expiry(INDEX_EXPIRIES, now=at(2026, 10, 5)) == date(2026, 10, 27)


def test_front_month_rolls_at_the_expiry_instant_not_at_midnight():
    """
    On expiry day the expiring contract is still the front until 15:30; after
    that it quotes a settlement, not a market.
    """
    assert front_month_expiry(INDEX_EXPIRIES, now=at(2026, 10, 27, 15, 0)) == date(2026, 10, 27)
    assert front_month_expiry(INDEX_EXPIRIES, now=at(2026, 10, 27, 15, 31)) == date(2026, 11, 24)


def test_no_weekday_is_assumed():
    """Thursday-era and Tuesday-era calendars both resolve from the listing alone."""
    thursday_era = ["02-01-2025", "09-01-2025", "30-01-2025", "27-02-2025"]
    assert front_month_expiry(thursday_era, now=at(2025, 1, 3)) == date(2025, 1, 30)


def test_stock_listings_are_monthly_already():
    stock = ["27-10-2026", "24-11-2026", "29-12-2026"]
    assert front_month_expiry(stock, now=at(2026, 10, 28)) == date(2026, 11, 24)


def test_unparseable_or_empty_listing_yields_no_contract():
    assert front_month_expiry([], now=at(2026, 10, 5)) is None
    assert front_month_expiry(["not-a-date"], now=at(2026, 10, 5)) is None


def test_fyers_symbol_format():
    assert fyers_future_symbol("NIFTY", date(2026, 10, 27)) == "NSE:NIFTY26OCTFUT"
    assert fyers_future_symbol("banknifty", date(2027, 1, 26)) == "NSE:BANKNIFTY27JANFUT"
    assert fyers_future_symbol("RELIANCE", date(2030, 12, 31)) == "NSE:RELIANCE30DECFUT"


# ── Carry ─────────────────────────────────────────────────────────────────────

def test_implied_carry_recovers_the_rate_that_priced_the_future():
    T = 30 / 365
    assert abs(implied_carry(24000 * math.exp(0.055 * T), 24000, T) - 0.055) < 1e-12


def test_backwardation_is_a_negative_carry():
    assert implied_carry(23900, 24000, 30 / 365) < 0


def test_carry_is_refused_near_expiry_and_on_bad_input():
    """A few points over a few hours annualises into nonsense."""
    assert implied_carry(24010, 24000, 0.5 / 365) is None
    assert implied_carry(None, 24000, 0.1) is None
    assert implied_carry(24000, 0, 0.1) is None


# ── Batched quotes ────────────────────────────────────────────────────────────

class FakeFyers:
    def __init__(self, entries):
        self.entries = entries
        self.calls = []

    def quotes(self, payload):
        self.calls.append(payload)
        return {"s": "ok", "d": self.entries}


def test_a_rejected_symbol_drops_only_itself():
    from fyers_client import fetch_quotes

    fy = FakeFyers([
        {"n": "NSE:NIFTY50-INDEX", "s": "ok", "v": {"lp": 24010.5}},
        {"n": "NSE:NIFTY26OCTFUT", "s": "error", "v": {"errmsg": "invalid symbol"}},
    ])
    got = fetch_quotes(fy, ["NSE:NIFTY50-INDEX", "NSE:NIFTY26OCTFUT"])
    assert got == {"NSE:NIFTY50-INDEX": 24010.5}
    assert fy.calls == [{"symbols": "NSE:NIFTY50-INDEX,NSE:NIFTY26OCTFUT"}]


def test_a_zero_price_is_not_a_quote():
    from fyers_client import fetch_quotes

    fy = FakeFyers([{"n": "NSE:NIFTY26OCTFUT", "s": "ok", "v": {"lp": 0}}])
    assert fetch_quotes(fy, ["NSE:NIFTY26OCTFUT"]) == {}


# ── The recorder ──────────────────────────────────────────────────────────────

def _patch_recorder(monkeypatch, quotes: dict[str, float]):
    import recorder.service as svc

    monkeypatch.setattr(svc, "now_ist", lambda: at(2026, 10, 5, 11, 0))
    monkeypatch.setattr(svc, "fetch_expiry_list", lambda f, s: [
        {"date": d, "expiry": 1_800_000_000 + i} for i, d in enumerate(INDEX_EXPIRIES)])
    monkeypatch.setattr(svc, "fetch_quotes", lambda f, syms: {
        s: quotes[s] for s in syms if s in quotes})
    monkeypatch.setattr(svc, "fetch_option_chain", lambda f, s, e, strike_count: [
        {"strike": 24000.0, "option_type": "CE", "ltp": 120.0, "bid": 119.0, "ask": 121.0},
        {"strike": 24000.0, "option_type": "PE", "ltp": 110.0, "bid": 109.0, "ask": 111.0},
    ])
    svc.recorder_state.futures_captured = 0
    svc.recorder_state.futures_missed = 0
    svc.recorder_state.futures_warned = set()
    return svc


def test_recorder_writes_the_front_month_future_onto_the_snapshot(monkeypatch):
    svc = _patch_recorder(monkeypatch, {
        "NSE:NIFTY50-INDEX": 24000.0, "NSE:NIFTY26OCTFUT": 24095.0})
    snaps = svc._capture_symbol(None, "NIFTY", svc.RecorderConfig())

    assert len(snaps) == 1
    s = snaps[0]
    assert s.spot == 24000.0
    assert s.futures == 24095.0
    assert s.futures_expiry == "27-10-2026"
    # The option expiry is the weekly; the future is the monthly.
    assert s.expiry_date == "06-10-2026"
    assert svc.recorder_state.futures_captured == 1


def test_recorder_keeps_the_chain_when_the_future_does_not_quote(monkeypatch):
    svc = _patch_recorder(monkeypatch, {"NSE:NIFTY50-INDEX": 24000.0})
    snaps = svc._capture_symbol(None, "NIFTY", svc.RecorderConfig())

    assert len(snaps) == 1
    assert snaps[0].futures is None
    assert snaps[0].futures_expiry is None      # no expiry claimed for a missing price
    assert svc.recorder_state.futures_missed == 1
    assert svc.recorder_state.last_futures_symbol["NIFTY"] == "NSE:NIFTY26OCTFUT"


def test_recorder_still_fails_the_poll_without_spot(monkeypatch):
    """Spot is not best-effort: a chain with no spot is not worth recording."""
    import pytest

    svc = _patch_recorder(monkeypatch, {"NSE:NIFTY26OCTFUT": 24095.0})
    with pytest.raises(ValueError, match="No spot quote"):
        svc._capture_symbol(None, "NIFTY", svc.RecorderConfig())


# ── The store ─────────────────────────────────────────────────────────────────

def _snap(ts="2026-10-05T11:00:00+05:30", **kw) -> ChainSnapshot:
    return ChainSnapshot(
        ts=ts, session_date=ts[:10], session_phase=SessionPhase.CONTINUOUS,
        symbol=SYM, expiry_date="06-10-2026", expiry_epoch=1, spot=24000.0,
        rows=(ChainRow(strike=24000.0, option_type="CE", ltp=120.0),), **kw)


def test_futures_round_trip_through_the_store():
    from recorder.store import init_db, iter_snapshots, write_snapshot

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "m.db")
        init_db(db)
        write_snapshot(_snap(futures=24095.0, futures_expiry="27-10-2026"), db)
        [back] = list(iter_snapshots(SYM, "2026-10-05", db))
    assert back.futures == 24095.0
    assert back.futures_expiry == "27-10-2026"


def test_an_existing_database_gains_the_column_and_keeps_its_rows():
    """
    The store is append-only and cannot be rebuilt, so the schema change must be
    applied in place. Rows from before it read as "not captured".
    """
    from recorder.store import init_db, iter_snapshots, write_snapshot

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "old.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE chain_meta (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                session_date TEXT NOT NULL, session_phase TEXT NOT NULL,
                symbol TEXT NOT NULL, expiry_date TEXT NOT NULL,
                expiry_epoch INTEGER NOT NULL, spot REAL, futures REAL,
                row_count INTEGER, UNIQUE(ts, symbol, expiry_date));
            INSERT INTO chain_meta (ts, session_date, session_phase, symbol,
                expiry_date, expiry_epoch, spot, futures, row_count)
            VALUES ('2026-09-24T11:00:00+05:30', '2026-09-24', 'CONTINUOUS',
                    'NIFTY', '30-09-2026', 1, 23900.0, NULL, 0);
        """)
        conn.commit()
        conn.close()

        init_db(db)                                  # migrates in place
        init_db(db)                                  # and is idempotent
        write_snapshot(_snap(futures=24095.0, futures_expiry="27-10-2026"), db)

        [old] = list(iter_snapshots(SYM, "2026-09-24", db))
        [new] = list(iter_snapshots(SYM, "2026-10-05", db))
    assert old.spot == 23900.0 and old.futures is None and old.futures_expiry is None
    assert new.futures_expiry == "27-10-2026"


# ── EOD basis from the archive ────────────────────────────────────────────────

def _archive(tmp: str, futures_rows: list[tuple]) -> str:
    from bhavcopy.download import SCHEMA

    db = os.path.join(tmp, "eod.db")
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.executemany("""
        INSERT INTO daily_future (trad_dt, symbol, expiry_dt, close, volume,
                                  underlying, era)
        VALUES (?, ?, ?, ?, ?, ?, 'udiff')
    """, futures_rows)
    conn.commit()
    conn.close()
    return db


def test_basis_uses_the_front_contract_and_annualises_it():
    with tempfile.TemporaryDirectory() as tmp:
        db = _archive(tmp, [
            ("2026-10-05", SYM, "2026-10-27", 24095.0, 5000, 24000.0),
            ("2026-10-05", SYM, "2026-11-24", 24210.0, 900, 24000.0),
        ])
        [row] = load_basis_history(SYM, db)

    assert row["expiry"] == "2026-10-27"             # front, not next
    assert row["basis_pts"] == 95.0
    T = row["days"] / 365.0
    assert abs(row["carry"] - math.log(24095 / 24000) / T) < 1e-5


def test_an_expiring_contract_is_never_the_front_on_its_own_expiry_day():
    """On expiry day the archive's close for it is a settlement, not a price."""
    with tempfile.TemporaryDirectory() as tmp:
        db = _archive(tmp, [
            ("2026-10-27", SYM, "2026-10-27", 24000.0, 9000, 24000.0),
            ("2026-10-27", SYM, "2026-11-24", 24110.0, 4000, 24000.0),
        ])
        [row] = load_basis_history(SYM, db)
    assert row["expiry"] == "2026-11-24"


def test_an_untraded_front_contract_is_skipped_not_used():
    with tempfile.TemporaryDirectory() as tmp:
        db = _archive(tmp, [
            ("2026-10-05", SYM, "2026-10-27", 24095.0, 0, 24000.0),
            ("2026-10-06", SYM, "2026-10-27", 24130.0, 4000, 24040.0),
        ])
        series = load_basis_history(SYM, db)
    assert [r["date"] for r in series] == ["2026-10-06"]


def test_a_date_with_no_published_underlying_is_skipped():
    """Pre-2024-07-08 files: backing spot out of the future needs the carry itself."""
    with tempfile.TemporaryDirectory() as tmp:
        db = _archive(tmp, [("2024-05-02", SYM, "2024-05-30", 22700.0, 5000, None)])
        assert load_basis_history(SYM, db) == []


def test_missing_archive_yields_an_empty_series_and_an_explanation():
    with tempfile.TemporaryDirectory() as tmp:
        assert load_basis_history(SYM, os.path.join(tmp, "absent.db")) == []
    assert summarise_basis([])["observations"] == 0


def test_eod_snapshots_carry_the_front_future():
    """The EOD adapter now reads the futures the archive has held all along."""
    from bhavcopy.snapshots import iter_eod_snapshots

    with tempfile.TemporaryDirectory() as tmp:
        db = _archive(tmp, [("2026-10-05", SYM, "2026-10-27", 24095.0, 5000, 24000.0)])
        conn = sqlite3.connect(db)
        conn.execute("""
            INSERT INTO daily_option (trad_dt, symbol, expiry_dt, strike,
                option_type, close, volume, oi, chg_oi, underlying, era)
            VALUES ('2026-10-05', 'NIFTY', '2026-10-06', 24000, 'CE', 120.0,
                    500, 1000, 10, 24000.0, 'udiff')
        """)
        conn.commit()
        conn.close()

        [snap] = list(iter_eod_snapshots(SYM, "2026-10-05", db))
    assert snap.futures == 24095.0
    assert snap.futures_expiry == "27-10-2026"
