# optionslens/backend/tests/test_auth_validate.py
"""
/api/auth/validate must accept a good token and auto-start the recorder.

Regression: as a sync endpoint it ran in a worker thread with no event loop,
so start_recorder's asyncio.create_task raised "no running event loop" and
every valid token was rejected with 401.
"""
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main                                   # noqa: E402
from routers import recorder as recorder_router  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    started = []

    async def fake_recorder_task(token, cfg):
        started.append(token)
        await asyncio.sleep(3600)

    monkeypatch.setattr(main, "get_fyers", lambda token: object())
    monkeypatch.setattr(main, "fetch_quote", lambda fyers, sym: 24_000.0)
    monkeypatch.setattr(main, "register_token", lambda token: None)
    monkeypatch.setattr(recorder_router, "recorder_task", fake_recorder_task)
    monkeypatch.setattr(recorder_router, "_recorder_handle", None)
    # A no-op lifespan: the real one opens the databases and starts the
    # scheduler. The `with` block keeps one event loop alive across requests,
    # as uvicorn does, so a task started by one request is still running when
    # the next arrives.
    @asynccontextmanager
    async def no_lifespan(app):
        yield
    monkeypatch.setattr(main.app.router, "lifespan_context", no_lifespan)
    with TestClient(main.app) as tc:
        tc.started = started
        yield tc


def _get(client, token="good-token"):
    return client.get("/api/auth/validate", headers={"Authorization": f"Bearer {token}"})


def test_valid_token_is_accepted_and_starts_the_recorder(client):
    r = _get(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True
    assert body["recorder_started"] is True


def test_second_validation_does_not_start_a_second_recorder(client):
    assert _get(client).json()["recorder_started"] is True
    second = _get(client)
    assert second.status_code == 200
    assert second.json()["recorder_started"] is False


def test_bad_token_is_still_rejected(client, monkeypatch):
    def refuse(fyers, sym):
        raise ValueError("invalid token")
    monkeypatch.setattr(main, "fetch_quote", refuse)
    r = _get(client)
    assert r.status_code == 401
    assert "invalid token" in r.json()["detail"]


def test_recorder_failure_does_not_reject_a_valid_token(client, monkeypatch):
    def boom(token):
        raise RuntimeError("recorder exploded")
    monkeypatch.setattr(main, "start_recorder", boom)
    r = _get(client)
    assert r.status_code == 200
    assert r.json()["recorder_started"] is False
