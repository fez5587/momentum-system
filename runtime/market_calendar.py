"""NYSE trading-calendar helpers — no external dependency.

``pandas_market_calendars`` isn't installed, and the live loop previously gated
trading on ``weekday() < 5`` alone. That treated market holidays as normal
sessions: on a full closure the EOD flatten would still cancel the protective
bracket legs (it runs in the 15:50-16:00 window) and then fail to fill the
market sell (exchange closed), leaving positions NAKED over the holiday — the
exact naked-stop failure class we already fixed elsewhere.

Dates are hardcoded NYSE full-day closures + early closes (1pm). Verified for
2026 against the NYSE 2026 holiday calendar; 2027 is computed from the same
rules and should be re-confirmed when the year is current. Update annually.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# Full-day market closures (exchange shut).
_HOLIDAYS: frozenset[date] = frozenset({
    # 2026 — confirmed
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027 — computed from NYSE rules, re-confirm when current
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
})

# Early-close days: regular session ends 13:00 ET instead of 16:00.
_EARLY_CLOSE: frozenset[date] = frozenset({
    date(2026, 7, 2), date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
})


def is_market_holiday(d: date) -> bool:
    """True if the exchange is fully closed on this calendar date."""
    return d in _HOLIDAYS


def is_early_close(d: date) -> bool:
    """True if this is a 1pm-ET early-close session."""
    return d in _EARLY_CLOSE


def is_trading_day(d: date) -> bool:
    """A regular weekday that isn't a full-closure holiday."""
    return d.weekday() < 5 and d not in _HOLIDAYS


def session_close_hm(d: date) -> tuple[int, int] | None:
    """(hour, minute) ET of the regular-session close, or None if not a
    trading day. 13:00 on early-close days, else 16:00."""
    if not is_trading_day(d):
        return None
    return (13, 0) if d in _EARLY_CLOSE else (16, 0)


def is_regular_hours(now_et: datetime | None = None) -> bool:
    """True iff a regular trading session is open right now (ET), honouring
    weekends, full closures, and early closes."""
    n = now_et or datetime.now(_ET)
    close = session_close_hm(n.date())
    if close is None:
        return False
    return (9, 30) <= (n.hour, n.minute) < close
