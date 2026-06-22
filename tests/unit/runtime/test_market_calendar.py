"""NYSE calendar: the holiday gate that keeps EOD flatten from going naked."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from runtime.market_calendar import (
    days_to_next_session,
    eod_flatten_status,
    is_early_close,
    is_market_holiday,
    is_regular_hours,
    is_trading_day,
    next_trading_day,
    session_close_hm,
)

ET = ZoneInfo("America/New_York")


def _et(d, h, m):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ET)


def test_next_trading_day_skips_holiday_weekend():
    assert next_trading_day(date(2026, 6, 18)) == date(2026, 6, 22)   # skip Juneteenth + wknd
    assert next_trading_day(date(2026, 6, 16)) == date(2026, 6, 17)   # plain weeknight
    assert next_trading_day(date(2026, 6, 26)) == date(2026, 6, 29)   # Fri -> Mon


def test_days_to_next_session():
    assert days_to_next_session(date(2026, 6, 16)) == 1   # Tue -> Wed
    assert days_to_next_session(date(2026, 6, 18)) == 4   # Thu -> Mon (Juneteenth)
    assert days_to_next_session(date(2026, 6, 26)) == 3   # Fri -> Mon


def test_eod_flatten_normal_day_window():
    d = date(2026, 6, 16)  # Tue, next session is tomorrow
    assert eod_flatten_status(_et(d, 15, 40)) == (False, False)  # before window
    assert eod_flatten_status(_et(d, 15, 55)) == (True, False)   # in [15:55, 16:00)
    assert eod_flatten_status(_et(d, 16, 0)) == (False, False)   # at/after close


def test_eod_flatten_widens_before_holiday():
    d = date(2026, 6, 18)  # Thu before the 4-day Juneteenth gap -> pre-closure
    # 15:45 is INSIDE the widened [15:40, 16:00) window but OUTSIDE the normal 5-min one
    assert eod_flatten_status(_et(d, 15, 45)) == (True, True)
    assert eod_flatten_status(_et(d, 15, 45), normal_lead_min=5, pre_closure_lead_min=5)[0] is False
    assert eod_flatten_status(_et(d, 15, 30))[0] is False        # before even the wide window


def test_eod_flatten_fires_on_early_close_halfday():
    # July 2 2026: early close 13:00 AND precedes the July 3 holiday + weekend.
    # A fixed 15:55 would NEVER fire (15:55 > 13:00) -> the naked-carry bug.
    d = date(2026, 7, 2)
    assert eod_flatten_status(_et(d, 12, 45)) == (True, True)     # in [12:40, 13:00)
    assert eod_flatten_status(_et(d, 15, 55))[0] is False         # well after the 1pm close


def test_eod_flatten_skips_closed_days():
    assert eod_flatten_status(_et(date(2026, 6, 19), 15, 55)) == (False, False)  # Juneteenth
    assert eod_flatten_status(_et(date(2026, 6, 20), 15, 55)) == (False, False)  # Sat


def test_juneteenth_2026_is_a_holiday():
    # the case that prompted this: Fri Jun 19 2026 is a weekday but NYSE is shut
    assert date(2026, 6, 19).weekday() < 5
    assert is_market_holiday(date(2026, 6, 19))
    assert not is_trading_day(date(2026, 6, 19))
    assert session_close_hm(date(2026, 6, 19)) is None


def test_surrounding_days_trade():
    assert is_trading_day(date(2026, 6, 18))   # Thu before
    assert is_trading_day(date(2026, 6, 22))   # Mon after (06-20/21 weekend)


def test_weekend_not_trading():
    assert not is_trading_day(date(2026, 6, 20))  # Sat
    assert not is_trading_day(date(2026, 6, 21))  # Sun


def test_full_session_open_closed():
    d = date(2026, 6, 18)  # normal trading day
    assert not is_regular_hours(datetime(d.year, d.month, d.day, 9, 0, tzinfo=ET))
    assert is_regular_hours(datetime(d.year, d.month, d.day, 10, 0, tzinfo=ET))
    assert not is_regular_hours(datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET))


def test_holiday_never_open():
    j = date(2026, 6, 19)
    assert not is_regular_hours(datetime(j.year, j.month, j.day, 11, 0, tzinfo=ET))


def test_early_close_ends_at_1pm():
    d = date(2026, 7, 2)  # half day
    assert is_early_close(d)
    assert session_close_hm(d) == (13, 0)
    assert is_regular_hours(datetime(d.year, d.month, d.day, 12, 0, tzinfo=ET))
    assert not is_regular_hours(datetime(d.year, d.month, d.day, 14, 0, tzinfo=ET))
