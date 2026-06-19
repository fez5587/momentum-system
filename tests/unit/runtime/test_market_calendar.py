"""NYSE calendar: the holiday gate that keeps EOD flatten from going naked."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from runtime.market_calendar import (
    is_early_close,
    is_market_holiday,
    is_regular_hours,
    is_trading_day,
    session_close_hm,
)

ET = ZoneInfo("America/New_York")


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
