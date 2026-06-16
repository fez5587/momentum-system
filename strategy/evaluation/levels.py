"""Key intraday levels: VWAP, EMAs, premarket high/low, opening range.

Pure functions over OHLCV DataFrames — no broker or UI dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class KeyLevels:
    """Computed key levels for a symbol's session."""

    vwap: float | None = None
    ema_9: float | None = None
    ema_20: float | None = None
    premarket_high: float | None = None
    premarket_low: float | None = None
    opening_range_high: float | None = None
    opening_range_low: float | None = None
    high_of_day: float | None = None
    low_of_day: float | None = None
    previous_close: float | None = None
    extras: dict = field(default_factory=dict)


def calculate_vwap(bars: pd.DataFrame) -> pd.Series:
    """Cumulative volume-weighted average price over the given bars."""
    if bars.empty:
        return pd.Series(dtype=float)
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    vol = bars["volume"].astype(float).clip(lower=0)
    cum_vol = vol.cumsum().replace(0, np.nan)
    vwap = (typical * vol).cumsum() / cum_vol
    return vwap.ffill().fillna(typical)


def calculate_ema(values: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    if values.empty:
        return pd.Series(dtype=float)
    return values.ewm(span=period, adjust=False).mean()


def compute_key_levels(
    bars: pd.DataFrame,
    previous_close: float | None = None,
    opening_range_minutes: int = 5,
) -> KeyLevels:
    """Compute key levels from a session's minute bars.

    Args:
        bars: minute bars (chronological) with open/high/low/close/volume and
            optionally is_premarket / is_regular_hours boolean columns.
        previous_close: prior session close, if known.
        opening_range_minutes: bars to include in the opening range.
    """
    levels = KeyLevels(previous_close=previous_close)
    if bars.empty:
        return levels

    bars = bars.reset_index(drop=True)
    vwap_series = calculate_vwap(bars)
    levels.vwap = float(vwap_series.iloc[-1])
    closes = bars["close"].astype(float)
    levels.ema_9 = float(calculate_ema(closes, 9).iloc[-1])
    levels.ema_20 = float(calculate_ema(closes, 20).iloc[-1])
    levels.high_of_day = float(bars["high"].max())
    levels.low_of_day = float(bars["low"].min())

    if "is_premarket" in bars.columns:
        pm = bars[bars["is_premarket"].astype(bool)]
        if not pm.empty:
            levels.premarket_high = float(pm["high"].max())
            levels.premarket_low = float(pm["low"].min())

    if "is_regular_hours" in bars.columns:
        rth = bars[bars["is_regular_hours"].astype(bool)]
    else:
        rth = bars
    if not rth.empty:
        opening = rth.head(opening_range_minutes)
        levels.opening_range_high = float(opening["high"].max())
        levels.opening_range_low = float(opening["low"].min())

    return levels


def get_trigger_levels(levels: KeyLevels) -> list[tuple[str, float]]:
    """Candidate breakout trigger levels, ordered by priority."""
    out: list[tuple[str, float]] = []
    if levels.high_of_day is not None:
        out.append(("high_of_day", levels.high_of_day))
    if levels.premarket_high is not None:
        out.append(("premarket_high", levels.premarket_high))
    if levels.opening_range_high is not None:
        out.append(("opening_range_high", levels.opening_range_high))
    return out


def get_stop_levels(levels: KeyLevels, entry_price: float) -> list[tuple[str, float]]:
    """Candidate protective stop levels below the entry, ordered tightest first."""
    candidates: list[tuple[str, float]] = []
    if levels.vwap is not None and levels.vwap < entry_price:
        candidates.append(("vwap", levels.vwap))
    if levels.ema_9 is not None and levels.ema_9 < entry_price:
        candidates.append(("ema_9", levels.ema_9))
    if levels.opening_range_low is not None and levels.opening_range_low < entry_price:
        candidates.append(("opening_range_low", levels.opening_range_low))
    if levels.low_of_day is not None and levels.low_of_day < entry_price:
        candidates.append(("low_of_day", levels.low_of_day))
    return sorted(candidates, key=lambda kv: entry_price - kv[1])
