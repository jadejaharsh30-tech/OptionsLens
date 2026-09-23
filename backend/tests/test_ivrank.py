# optionslens/backend/tests/test_ivrank.py
"""
/api/ivrank with a fake broker: the endpoint must report 30-day
constant-maturity ATM IV (not the nearest expiry's) and rank it as a
percentile of the stored 30-day history.
"""
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_iv                                                  # noqa: E402
from auth import get_token                                      # noqa: E402
from config import RISK_FREE_RATE                               # noqa: E402
from iv_engine import black76_price                             # noqa: E402
from market_hours import now_ist, time_to_expiry                # noqa: E402
from routers import ivrank                                      # noqa: E402
from snapshot_store import init_db, write_atm_iv_many           # noqa: E402

SPOT = 24_000.0
TODAY = now_ist().date()
# Front weekly at 5 days carries a vol spike; the pair either side of 30 days
# is flat at 15%. A correct endpoint reports 15, the old one reported ~30.
VOLS = {5: 0.30, 16: 0.15, 44: 0.15, 72: 0.15}


def _ddmmyyyy(d: date) -> str:
    return d.strftime("%d-%m-%Y")


EXPIRIES = [{"expiry": days, "date": _ddmmyyyy(TODAY + timedelta(days=days))}
            for days in sorted(VOLS)]


class FakeBroker:
    def __init__(self):
        self.chains_fetched = []

    def chain(self, _fyers, _symbol, expiry_epoch, strike_count=6):
        self.chains_fetched.append(expiry_epoch)
        exp = next(e for e in EXPIRIES if e["expiry"] == expiry_epoch)
        T = time_to_expiry(exp["date"])
        F = SPOT * math.exp(RISK_FREE_RATE * T)
        vol = VOLS[expiry_epoch]
        return [{"strike": float(k), "option_type": ot,
                 "ltp": round(black76_price(F, k, T, RISK_FREE_RATE, vol, ot), 2),
                 "bid": 0, "ask": 0, "oi": 0}
                for k in range(23_700, 24_350, 50) for ot in ("CE", "PE")]


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "app.db")
    init_db(db)
    broker = FakeBroker()
    monkeypatch.setattr(ivrank, "DB_PATH", db)
    monkeypatch.setattr(ivrank, "get_fyers", lambda token: object())
    monkeypatch.setattr(ivrank, "fetch_quote", lambda fyers, sym: SPOT)
    monkeypatch.setattr(ivrank, "fetch_expiry_list", lambda fyers, sym: EXPIRIES)
    monkeypatch.setattr(ivrank, "fetch_historical_prices", lambda fyers, sym, days=90: [])
    monkeypatch.setattr(live_iv, "fetch_option_chain", broker.chain)

    app = FastAPI()
    app.include_router(ivrank.router)
    app.dependency_overrides[get_token] = lambda: "test-token"
    tc = TestClient(app)
    tc.db, tc.broker = db, broker
    return tc


def _seed_history(db: str, n: int, start_iv: float = 0.10, step: float = 0.001):
    rows = []
    for i in range(n):
        d = TODAY - timedelta(days=n - i)
        for tenor in (16, 44):
            rows.append((d.isoformat(), "NIFTY", _ddmmyyyy(d + timedelta(days=tenor)),
                         start_iv + i * step))
    write_atm_iv_many(db, rows, source="test")


def test_reports_thirty_day_iv_not_the_front_expiry(client):
    body = client.get("/api/ivrank/NIFTY").json()
    assert body["current_iv"] == pytest.approx(15.0, abs=0.05)
    assert body["iv_tenor_days"] == 30
    assert body["rank_method"] == "percentile"


def test_fetches_only_the_two_bracketing_chains(client):
    body = client.get("/api/ivrank/NIFTY").json()
    assert sorted(client.broker.chains_fetched) == [16, 44]
    assert body["expiries_used"] == [EXPIRIES[1]["date"], EXPIRIES[2]["date"]]


def test_rank_is_a_percentile_of_the_stored_history(client):
    _seed_history(client.db, 40)                  # 10.0% .. 13.9%, all below 15%
    body = client.get("/api/ivrank/NIFTY").json()
    assert body["iv_rank"] == pytest.approx(100.0)
    assert body["history_days"] == 40
    assert body["note"] is None


def test_rank_sits_mid_history_when_iv_is_mid_history(client):
    _seed_history(client.db, 40, start_iv=0.13, step=0.001)   # 13.0% .. 16.9%
    body = client.get("/api/ivrank/NIFTY").json()
    assert 45.0 < body["iv_rank"] < 55.0


def test_thin_history_explains_how_to_fill_it(client):
    _seed_history(client.db, 5)
    body = client.get("/api/ivrank/NIFTY").json()
    assert body["iv_rank"] is None
    assert body["history_days"] == 5
    assert "bhavcopy.importer" in body["note"]


def test_unknown_symbol_is_rejected(client):
    assert client.get("/api/ivrank/NOPE").status_code == 400
