# optionslens/backend/tests/test_bhavcopy_download.py
"""bhavcopy.download: routine updates must never turn an unpublished day into a holiday."""
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bhavcopy import download  # noqa: E402


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(download, "fetch", lambda url, retries=3, timeout=90: None)
    c = download.connect(tmp_path / "eod.db")
    yield c
    c.close()


def _logged(c):
    return dict(c.execute("SELECT trad_dt, status FROM ingest_log").fetchall())


def test_recent_missing_file_is_left_for_the_next_run(conn):
    day = download.today_ist() - timedelta(days=1)
    status, *_ = download.ingest_day(conn, day, None, False)
    assert status == "pending"
    assert day.isoformat() not in _logged(conn)
    assert not download.already_done(conn, day)


def test_old_missing_file_is_recorded_as_a_holiday(conn):
    day = download.today_ist() - timedelta(days=30)
    status, *_ = download.ingest_day(conn, day, None, False)
    assert status == "no_file"
    assert _logged(conn)[day.isoformat()] == "no_file"
    assert download.already_done(conn, day)


def test_today_is_accepted_as_a_date():
    assert download.parse_day("today") == download.today_ist()
    assert download.parse_day("TODAY") == download.today_ist()
    assert str(download.parse_day("2026-09-10")) == "2026-09-10"


def test_run_to_today_reports_pending_without_logging(conn, capsys):
    today = download.today_ist()
    download.run(conn, today - timedelta(days=2), today, None, False, 0)
    out = capsys.readouterr().out
    weekdays = [today - timedelta(days=i) for i in range(3)
                if (today - timedelta(days=i)).weekday() < 5]
    if weekdays:
        assert "not published yet" in out
    assert _logged(conn) == {}
