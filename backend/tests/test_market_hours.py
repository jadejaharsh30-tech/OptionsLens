"""
Session-phase tests, written against the CAS regime (live 3 Aug 2026).

These pin down the boundaries that CAS moved: continuous trading ends 15:15,
derivatives run to 15:40, and cash prices are unusable during the auction.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_hours import (  # noqa: E402
    IST, SessionPhase, get_session_phase, is_cash_price_reliable,
    is_continuous_session, is_derivatives_open, is_recording_window,
    is_trading_day, seconds_until_next_tick, time_to_expiry,
)


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


# 2026-09-11 is a Friday; 2026-09-12 a Saturday.
FRIDAY = (2026, 9, 11)


def test_phases_across_the_trading_day():
    assert get_session_phase(at(*FRIDAY, 8, 30))  is SessionPhase.CLOSED
    assert get_session_phase(at(*FRIDAY, 9, 5))   is SessionPhase.PRE_OPEN
    assert get_session_phase(at(*FRIDAY, 12, 0))  is SessionPhase.CONTINUOUS
    assert get_session_phase(at(*FRIDAY, 15, 20)) is SessionPhase.CAS_WINDOW
    assert get_session_phase(at(*FRIDAY, 15, 36)) is SessionPhase.POST_CAS
    assert get_session_phase(at(*FRIDAY, 15, 45)) is SessionPhase.CLOSED


def test_continuous_session_ends_at_1515_not_1530():
    """CAS moved the cash close forward by 15 minutes."""
    assert is_continuous_session(at(*FRIDAY, 15, 14))
    assert not is_continuous_session(at(*FRIDAY, 15, 16))


def test_derivatives_stay_open_past_the_old_1530_close():
    """The old is_market_open() cutoff dropped this window entirely."""
    assert is_derivatives_open(at(*FRIDAY, 15, 30))
    assert is_derivatives_open(at(*FRIDAY, 15, 39))
    assert not is_derivatives_open(at(*FRIDAY, 15, 41))


def test_recorder_captures_through_the_auction():
    assert is_recording_window(at(*FRIDAY, 15, 20))
    assert is_recording_window(at(*FRIDAY, 15, 36))
    assert not is_recording_window(at(*FRIDAY, 16, 0))


def test_cash_price_unreliable_during_auction():
    """Guards against snapshotting a stale pre-auction print as a close."""
    assert is_cash_price_reliable(at(*FRIDAY, 14, 0))
    assert not is_cash_price_reliable(at(*FRIDAY, 15, 20))
    # The existing 15:20 IST daily snapshot lands inside the auction window.
    assert not is_cash_price_reliable(at(*FRIDAY, 15, 20))


def test_weekend_is_not_a_trading_day():
    assert is_trading_day(at(*FRIDAY, 12, 0).date())
    assert not is_trading_day(at(2026, 9, 12, 12, 0).date())
    assert get_session_phase(at(2026, 9, 12, 12, 0)) is SessionPhase.CLOSED


# ── time_to_expiry ────────────────────────────────────────────────────────────
# The old whole-day version returned exactly 0.0 for the whole of expiry day,
# which disabled IV and Greeks across the app on the day they matter most.

def test_expiry_day_still_has_time_value_before_1530():
    """The regression that killed 0DTE: T must be > 0 during expiry day."""
    t = time_to_expiry("11-09-2026", at(*FRIDAY, 10, 0))
    assert t > 0
    # 5.5 hours to 15:30, in years.
    assert abs(t - 5.5 / (365 * 24)) < 1e-9


def test_expiry_day_shrinks_monotonically_through_the_session():
    morning   = time_to_expiry("11-09-2026", at(*FRIDAY, 10, 0))
    afternoon = time_to_expiry("11-09-2026", at(*FRIDAY, 14, 0))
    assert morning > afternoon > 0


def test_expired_after_1530_on_expiry_day():
    assert time_to_expiry("11-09-2026", at(*FRIDAY, 15, 31)) == 0.0
    assert time_to_expiry("10-09-2026", at(*FRIDAY, 10, 0)) == 0.0


def test_sub_minute_remainder_counts_as_expired():
    """Near-zero T only produces solver noise, never tradeable time value."""
    assert time_to_expiry("11-09-2026", at(*FRIDAY, 15, 29)) == 0.0


def test_future_expiry_is_roughly_calendar_days():
    # 11-09 10:00 → 17-09 15:30 is 6 days plus 5.5 hours.
    t = time_to_expiry("17-09-2026", at(*FRIDAY, 10, 0))
    assert 6.0 / 365 < t < 7.0 / 365
    assert abs(t - (6 + 5.5 / 24) / 365) < 1e-9


def test_alternate_fyers_date_format_is_accepted():
    assert time_to_expiry("17-Sep-2026", at(*FRIDAY, 10, 0)) > 0


def test_tick_alignment_lands_on_interval_boundaries():
    # 12:00:20 with a 60s interval → 40s until the next clean minute.
    assert seconds_until_next_tick(60, datetime(2026, 9, 11, 12, 0, 20, tzinfo=IST)) == 40
    # Never returns 0, which would spin the loop.
    assert seconds_until_next_tick(60, datetime(2026, 9, 11, 12, 0, 0, tzinfo=IST)) > 0
