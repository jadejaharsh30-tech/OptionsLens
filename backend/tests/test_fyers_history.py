# optionslens/backend/tests/test_fyers_history.py
"""fetch_historical_prices against a fake Fyers client."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fyers_client import fetch_historical_prices  # noqa: E402


class FakeFyers:
    def __init__(self, resp):
        self.resp, self.request = resp, None

    def history(self, req):
        self.request = req
        return self.resp


def test_returns_closes_oldest_first_with_a_valid_epoch_range():
    fake = FakeFyers({"s": "ok", "candles": [[1, 0, 0, 0, 100.0, 0], [2, 0, 0, 0, 101.5, 0]]})
    assert fetch_historical_prices(fake, "NIFTY", days=90) == [100.0, 101.5]
    frm, to = int(fake.request["range_from"]), int(fake.request["range_to"])
    assert 0 < frm < to
    assert 89 * 86400 <= to - frm <= 91 * 86400


def test_broker_error_returns_empty_list():
    assert fetch_historical_prices(FakeFyers({"s": "error"}), "NIFTY") == []
