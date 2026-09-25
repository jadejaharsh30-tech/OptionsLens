"""
Router tests (roadmap item 54).

Only `/api/ivrank` had coverage; every other endpoint was untested, so a broken
route surfaced as a 500 in the browser. The auth-validate bug that rejected
every valid token for two weeks is exactly what this catches.

The broker is faked through `dependency_overrides` and monkeypatched module
functions — no token, no network. Stores are temp files, so nothing touches the
developer's real databases.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from auth import get_token  # noqa: E402

HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture(scope="module")
def client():
    """
    One app instance for the whole module.

    Module-scoped on purpose: the lifespan starts a module-level
    AsyncIOScheduler, and entering TestClient per test would start it again on
    a loop the previous client already closed.
    """
    main.app.dependency_overrides[get_token] = lambda: "test-token"
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def temp_stores(monkeypatch):
    """Point every store at throwaway files so tests cannot touch real data."""
    paths = {}
    for name in ("market", "app"):
        fd, p = tempfile.mkstemp(suffix=f"-{name}.db")
        os.close(fd)
        paths[name] = p

    import recorder.store as rstore
    import signals.store as sstore
    import snapshot_store
    import trading.store as tstore

    monkeypatch.setattr(rstore, "MARKET_DATA_DB", paths["market"])
    rstore.init_db(paths["market"])
    sstore.init_db(paths["market"])
    tstore.init_db(paths["market"])
    # The app store's tables too: an uninitialised file makes every read raise
    # "no such table", which is a different failure from "no data yet" and
    # would let a test pass on the wrong error path.
    snapshot_store.init_db(paths["app"])
    yield paths

    for p in paths.values():
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(p + suffix)
            except OSError:
                pass


# ── Unauthenticated surface ───────────────────────────────────────────────────

def test_health_needs_no_token():
    with TestClient(main.app) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_symbols_lists_configured_underlyings():
    with TestClient(main.app) as c:
        r = c.get("/api/symbols")
    assert r.status_code == 200
    keys = {s["key"] for s in r.json()["symbols"]}
    assert {"NIFTY", "BANKNIFTY"} <= keys
    # Lot sizes must come through lot_size_for, not raw config.
    nifty = next(s for s in r.json()["symbols"] if s["key"] == "NIFTY")
    assert nifty["lot_size"] > 0


def test_position_lab_is_the_only_open_data_endpoint():
    """Pure Black-Scholes, no broker call — so it must work without a token."""
    with TestClient(main.app) as c:
        r = c.post("/api/position-lab/calculate", json={
            "spot": 24000.0,
            "legs": [{"strike": 24000.0, "T": 0.08, "option_type": "CE",
                      "action": "BUY", "qty": 1, "iv": 0.15}],
        })
    assert r.status_code == 200
    body = r.json()
    assert 0 < body["net_greeks"]["delta"] < 1
    assert len(body["payoff"]["spot_range"]) == 101


def test_protected_endpoints_reject_a_missing_token():
    with TestClient(main.app) as c:
        for path in ("/api/recorder/status", "/api/backtest/signals",
                     "/api/trades", "/api/notify/status"):
            assert c.get(path).status_code in (401, 422), path


def test_bad_authorization_header_is_401():
    with TestClient(main.app) as c:
        r = c.get("/api/recorder/status", headers={"Authorization": "token-only"})
    assert r.status_code == 401


# ── Auth validate — the route that silently broke for two weeks ──────────────

def test_validate_returns_ok_when_the_broker_answers(client, monkeypatch):
    monkeypatch.setattr(main, "get_fyers", lambda t: object())
    monkeypatch.setattr(main, "fetch_quote", lambda f, s: 24512.5)
    monkeypatch.setattr(main, "register_token", lambda t: None)
    monkeypatch.setattr(main, "start_recorder", lambda t: True)

    r = client.get("/api/auth/validate", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["nifty_ltp"] == 24512.5
    assert body["recorder_started"] is True


def test_validate_survives_a_recorder_failure(client, monkeypatch):
    """
    The regression that rejected every valid token: the recorder start raised,
    and a catch-all turned a plumbing error into 'your token is bad'. A token
    Fyers has accepted must not be refused because a background task failed.
    """
    monkeypatch.setattr(main, "get_fyers", lambda t: object())
    monkeypatch.setattr(main, "fetch_quote", lambda f, s: 24000.0)
    monkeypatch.setattr(main, "register_token", lambda t: None)

    def boom(_t):
        raise RuntimeError("no running event loop")
    monkeypatch.setattr(main, "start_recorder", boom)

    r = client.get("/api/auth/validate", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["valid"] is True
    assert r.json()["recorder_started"] is False


def test_validate_rejects_a_broker_refusal(client, monkeypatch):
    def refuse(_f, _s):
        raise ValueError("invalid token")
    monkeypatch.setattr(main, "get_fyers", lambda t: object())
    monkeypatch.setattr(main, "fetch_quote", refuse)

    r = client.get("/api/auth/validate", headers=HEADERS)
    assert r.status_code == 401


# ── Recorder ──────────────────────────────────────────────────────────────────

def test_recorder_status_reports_phase_and_counters(client):
    r = client.get("/api/recorder/status", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"running", "session_phase", "rows_written",
                         "snapshots_written", "server_time_ist"}


def test_recorder_stop_when_not_running_is_not_an_error(client):
    r = client.post("/api/recorder/stop", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["stopped"] is False


def test_recorder_coverage_and_quality_run_on_an_empty_store(client, temp_stores):
    """Empty is a normal state on day one, not a 500."""
    assert client.get("/api/recorder/coverage", headers=HEADERS).status_code == 200
    assert client.get("/api/recorder/quality", headers=HEADERS).status_code == 200
    r = client.get("/api/recorder/dates", headers=HEADERS)
    assert r.status_code == 200 and r.json()["count"] == 0


# ── Backtest ──────────────────────────────────────────────────────────────────

def test_signals_endpoint_lists_every_registered_signal(client):
    r = client.get("/api/backtest/signals", headers=HEADERS)
    assert r.status_code == 200
    ids = {s["signal_id"] for s in r.json()["signals"]}
    assert {"gex_regime", "oi_short_buildup", "vrp",
            "term_structure", "skew_rr25"} <= ids
    for s in r.json()["signals"]:
        assert "default_params" in s and "version" in s


def test_data_readiness_is_honest_with_no_data(client, temp_stores):
    r = client.get("/api/backtest/data-readiness",
                   params={"symbol": "NIFTY"}, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["sessions_recorded"] == 0
    assert body["statistically_meaningful"] is False
    assert "No data recorded yet" in body["guidance"]


def test_run_refuses_an_unknown_signal(client):
    r = client.post("/api/backtest/run",
                    json={"signal_id": "not_a_signal", "symbol": "NIFTY"},
                    headers=HEADERS)
    assert r.status_code == 404


def test_run_refuses_when_no_data_is_recorded(client, temp_stores):
    r = client.post("/api/backtest/run",
                    json={"signal_id": "gex_regime", "symbol": "NIFTY"},
                    headers=HEADERS)
    assert r.status_code == 400
    assert "recorder must run" in r.json()["detail"].lower()


def test_every_series_backed_signal_is_handed_its_series(temp_stores, monkeypatch):
    """
    The regression that made `vrp` appear in the dropdown and skip every bar:
    the signal was registered, the UI listed it, and the router never passed the
    series it needs. A signal that silently never fires reads as "no setups
    today" rather than as a misconfiguration.

    Asserted generically, so a new series-backed signal cannot be added without
    either being wired in or failing here.
    """
    import routers.backtest as rb
    from signals import skew_signal, term_structure_signal, vrp_signal

    monkeypatch.setattr(rb, "DB_PATH", temp_stores["app"])

    for module in (vrp_signal, term_structure_signal, skew_signal):
        extras, notes = rb._build_extras("NIFTY", module.SIGNAL_ID)
        # The stores are empty, so the honest outcome is no series plus a note
        # that says which one is missing and what to run.
        assert module.EXTRAS_KEY not in extras
        assert any(module.SIGNAL_ID in n for n in notes), (
            f"{module.SIGNAL_ID} has no series and no note explaining why")


def test_build_extras_survives_one_leg_failing(temp_stores, monkeypatch):
    """A broken term-structure read must not also blank the VRP spread."""
    import routers.backtest as rb

    monkeypatch.setattr(rb, "DB_PATH", temp_stores["app"])
    monkeypatch.setattr(
        "term_structure.load_term_structure_history",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    extras, notes = rb._build_extras("NIFTY", "vrp")
    assert any("could not be loaded" in n for n in notes)
    assert any("IV/RV" in n for n in notes)       # the VRP pass still ran


def test_evaluations_endpoint_works_with_no_history(client, temp_stores):
    r = client.get("/api/backtest/evaluations/gex_regime", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["evaluations"] == 0


# ── Trades ────────────────────────────────────────────────────────────────────

def test_trades_list_is_empty_not_broken(client, temp_stores):
    r = client.get("/api/trades", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_trades_rejects_an_unknown_state_filter(client, temp_stores):
    r = client.get("/api/trades", params={"state": "NONSENSE"}, headers=HEADERS)
    assert r.status_code == 400


def test_journal_reports_emptiness(client, temp_stores):
    r = client.get("/api/trades/journal", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["trades"] == 0


def test_trade_lookup_404s_for_an_unknown_id(client, temp_stores):
    r = client.get("/api/trades/NOPE", headers=HEADERS)
    assert r.status_code == 404


def test_propose_rejects_an_unknown_symbol(client, temp_stores):
    r = client.post("/api/trades/propose", headers=HEADERS, json={
        "symbol": "NOTASYMBOL", "expiry_date": "29-10-2026",
        "expiry_epoch": 1793000000,
        "legs": [{"strike": 24000.0, "option_type": "CE", "action": "BUY"}],
    })
    assert r.status_code == 400


def test_propose_rejects_an_empty_leg_list(client, temp_stores):
    r = client.post("/api/trades/propose", headers=HEADERS, json={
        "symbol": "NIFTY", "expiry_date": "29-10-2026",
        "expiry_epoch": 1793000000, "legs": [],
    })
    assert r.status_code == 400


# ── Notifications ─────────────────────────────────────────────────────────────

def test_notify_status_reports_unconfigured_without_raising(client, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    from notify import reset_dispatcher
    reset_dispatcher()

    r = client.get("/api/notify/status", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["telegram_configured"] is False
    reset_dispatcher()


def test_notify_test_with_no_channels_is_a_clear_no_op(client, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    from notify import reset_dispatcher
    reset_dispatcher()

    r = client.post("/api/notify/test", headers=HEADERS)
    assert r.status_code == 200
    assert "No channels configured" in r.json()["note"]
    reset_dispatcher()


# ── Alert engine ──────────────────────────────────────────────────────────────

def test_alert_engine_status_runs(client):
    r = client.get("/api/alert-engine/status", headers=HEADERS)
    assert r.status_code == 200
    assert "running" in r.json()


def test_alert_engine_start_rejects_unknown_symbols(client, monkeypatch):
    import routers.alert_engine as ae
    monkeypatch.setattr(ae, "get_fyers", lambda t: object())
    monkeypatch.setattr(ae, "fetch_quote", lambda f, s: 24000.0)

    r = client.post("/api/alert-engine/start", headers=HEADERS,
                    json={"symbols": ["NOTASYMBOL"]})
    assert r.status_code == 400


# ── Chain / OI / surface, with a faked broker ────────────────────────────────

def _fake_chain(spot=24000.0):
    rows = []
    for k in range(int(spot) - 400, int(spot) + 401, 100):
        for opt in ("CE", "PE"):
            px = 120.0 if opt == "CE" else 110.0
            rows.append({"strike": float(k), "option_type": opt,
                         "oi": 5000.0, "oi_change": 100.0, "oi_change_pct": 2.0,
                         "prev_oi": 4900.0, "ltp": px,
                         "bid": px - 0.5, "ask": px + 0.5, "volume": 900.0})
    return rows


def test_chain_endpoint_returns_forward_and_greeks(client, monkeypatch):
    import routers.chain as rc
    monkeypatch.setattr(rc, "get_fyers", lambda t: object())
    monkeypatch.setattr(rc, "fetch_quote", lambda f, s: 24000.0)
    monkeypatch.setattr(rc, "fetch_option_chain",
                        lambda f, s, e, strike_count=20: _fake_chain())

    r = client.get("/api/chain/NIFTY", headers=HEADERS,
                   params={"expiry_epoch": 1793000000, "expiry_date": "29-10-2026"})
    assert r.status_code == 200
    body = r.json()
    assert body["forward"] is not None          # never priced off spot
    assert body["atm_strike"] == 24000.0
    assert any(row["iv"] is not None for row in body["chain"])


def test_chain_endpoint_rejects_an_unknown_symbol(client):
    r = client.get("/api/chain/NOTASYMBOL", headers=HEADERS,
                   params={"expiry_epoch": 1, "expiry_date": "29-10-2026"})
    assert r.status_code == 400


def test_oi_endpoint_returns_gex_and_max_pain(client, monkeypatch):
    import routers.oi as ro
    monkeypatch.setattr(ro, "get_fyers", lambda t: object())
    monkeypatch.setattr(ro, "fetch_quote", lambda f, s: 24000.0)
    monkeypatch.setattr(ro, "fetch_option_chain",
                        lambda f, s, e, strike_count=20: _fake_chain())

    r = client.get("/api/oi/NIFTY", headers=HEADERS,
                   params={"expiry_epoch": 1793000000, "expiry_date": "29-10-2026"})
    assert r.status_code == 200
    body = r.json()
    assert body["max_pain"] is not None
    assert body["pcr"] is not None
    assert len(body["gex_profile"]) > 0


def test_expiries_endpoint(client, monkeypatch):
    import routers.expiries as re_
    monkeypatch.setattr(re_, "get_fyers", lambda t: object())
    monkeypatch.setattr(re_, "fetch_expiry_list",
                        lambda f, s: [{"expiry": 1793000000, "date": "29-10-2026"}])

    r = client.get("/api/expiries/NIFTY", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["expiries"][0]["date"] == "29-10-2026"
