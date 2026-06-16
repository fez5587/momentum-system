"""Volume metrics: relative volume, time-of-day RVOL, float rotation."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class VolumeMetrics:
    """Enhanced volume metrics for a symbol."""

    session_volume: int = 0
    relative_volume: float = 0.0
    time_of_day_rvol: float = 0.0
    float_rotation: float = 0.0
    avg_bar_volume: float = 0.0
    last_bar_volume: int = 0


def calculate_time_of_day_rvol(
    session_volume: float,
    avg_daily_volume: float,
    minutes_elapsed: float,
    session_minutes: float = 390.0,
) -> float:
    """RVOL adjusted for how far through the session we are."""
    if avg_daily_volume <= 0 or minutes_elapsed <= 0:
        return 0.0
    fraction = min(1.0, max(minutes_elapsed / session_minutes, 1e-9))
    expected = avg_daily_volume * fraction
    return session_volume / expected if expected > 0 else 0.0


def calculate_float_rotation(session_volume: float, float_shares: float) -> float:
    """How many times the float has traded this session."""
    if float_shares <= 0:
        return 0.0
    return session_volume / float_shares


def calculate_enhanced_volume_metrics(
    bars: pd.DataFrame,
    avg_daily_volume: float | None = None,
    float_shares: float | None = None,
) -> VolumeMetrics:
    """Compute volume metrics from session minute bars."""
    metrics = VolumeMetrics()
    if bars.empty:
        return metrics

    vol = bars["volume"].astype(float)
    metrics.session_volume = int(vol.sum())
    metrics.avg_bar_volume = float(vol.mean())
    metrics.last_bar_volume = int(vol.iloc[-1])

    if avg_daily_volume and avg_daily_volume > 0:
        metrics.relative_volume = metrics.session_volume / avg_daily_volume
        metrics.time_of_day_rvol = calculate_time_of_day_rvol(
            metrics.session_volume, avg_daily_volume, minutes_elapsed=len(bars)
        )
    if float_shares and float_shares > 0:
        metrics.float_rotation = calculate_float_rotation(
            metrics.session_volume, float_shares
        )
    return metrics
