"""Synthetic minute-bar builders shared across the test suite."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

SESSION_DATE = datetime(2026, 6, 11).date()
# Bars are stored UTC-naive throughout the pipeline (ingestion converts to UTC
# then drops tzinfo). 13:30 UTC == 09:30 America/New_York in June (EDT), so this
# fixture exercises the same timezone path as live data: the watcher converts
# the bar timestamp UTC -> Eastern before the entry-cutoff check.
SESSION_OPEN = datetime(2026, 6, 11, 13, 30)  # 09:30 ET expressed in UTC


def make_bars(specs: list[tuple[float, float, float, float, int]],
              start: datetime | None = None,
              symbol: str = "TEST") -> pd.DataFrame:
    """Build a minute-bar frame from (open, high, low, close, volume) tuples."""
    start = start or SESSION_OPEN
    rows = []
    cum_pv = 0.0
    cum_v = 0
    for i, (o, h, l, c, v) in enumerate(specs):
        ts = start + timedelta(minutes=i)
        typical = (h + l + c) / 3.0
        cum_pv += typical * v
        cum_v += v
        rows.append(
            {
                "symbol": symbol,
                "timestamp": ts,
                "session_date": SESSION_DATE,
                "is_premarket": False,
                "is_regular_hours": True,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
                "vwap": cum_pv / cum_v if cum_v else c,
                "quality_score": 1.0,
            }
        )
    return pd.DataFrame(rows)


def bull_flag_bars(symbol: str = "TEST") -> pd.DataFrame:
    """Gap-up + impulse + light pullback + breakout: should evaluate ready.

    Pair with previous_close=10.0 and avg_daily_volume=500_000 so the gap
    (~25%) and relative volume criteria pass.
    """
    specs = [
        # base
        (12.50, 12.60, 12.45, 12.55, 60_000),
        (12.55, 12.65, 12.50, 12.60, 55_000),
        (12.60, 12.70, 12.55, 12.65, 50_000),
        # impulse leg on expanding volume
        (12.65, 12.95, 12.62, 12.92, 120_000),
        (12.92, 13.25, 12.90, 13.20, 150_000),
        (13.20, 13.55, 13.18, 13.50, 180_000),
        (13.50, 13.85, 13.48, 13.80, 200_000),
        # shallow pullback on lighter volume (holds above vwap)
        (13.80, 13.82, 13.62, 13.66, 70_000),
        (13.66, 13.70, 13.55, 13.60, 55_000),
        (13.60, 13.66, 13.52, 13.58, 45_000),
        # breakout bar through pullback high
        (13.58, 13.95, 13.56, 13.92, 220_000),
        (13.92, 14.05, 13.88, 14.00, 190_000),
    ]
    return make_bars(specs, symbol=symbol)


def fading_bars(symbol: str = "FADE") -> pd.DataFrame:
    """Steady downtrend below vwap: should evaluate blocked."""
    specs = []
    price = 10.0
    for i in range(15):
        o = price
        c = price - 0.08
        specs.append((o, o + 0.02, c - 0.03, c, 40_000))
        price = c
    return make_bars(specs, symbol=symbol)


def tiny_bars(symbol: str = "TINY") -> pd.DataFrame:
    """Too few bars: should block with insufficient_data."""
    return make_bars([(5.0, 5.1, 4.9, 5.05, 10_000)] * 3, symbol=symbol)
