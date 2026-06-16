"""Data quality scoring for minute-bar coverage and integrity."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class DataQualityScore:
    """Data quality assessment for a symbol's session bars."""

    score: float = 0.0
    grade: str = "F"
    bar_count: int = 0
    coverage: float = 0.0
    max_gap_minutes: float = 0.0
    has_zero_volume_run: bool = False
    issues: list[str] = field(default_factory=list)


def calculate_data_quality_score(
    bars: pd.DataFrame,
    expected_bars: int | None = None,
) -> DataQualityScore:
    """Score bar data 0..1 from coverage, gaps, and volume integrity."""
    dq = DataQualityScore()
    if bars.empty:
        dq.issues.append("no_bars")
        return dq

    dq.bar_count = len(bars)
    expected = expected_bars or max(len(bars), 30)
    dq.coverage = min(1.0, len(bars) / expected)

    score = dq.coverage

    ts = pd.to_datetime(bars["timestamp"]) if "timestamp" in bars.columns else None
    if ts is not None and len(ts) > 1:
        gaps = ts.diff().dt.total_seconds().fillna(60) / 60.0
        dq.max_gap_minutes = float(gaps.max())
        if dq.max_gap_minutes > 10:
            score -= 0.2
            dq.issues.append(f"gap_{dq.max_gap_minutes:.0f}m")

    zero_run = (bars["volume"].astype(float) <= 0).rolling(5).sum().max()
    if zero_run is not None and zero_run >= 5:
        dq.has_zero_volume_run = True
        score -= 0.2
        dq.issues.append("zero_volume_run")

    bad_prices = (
        (bars["high"] < bars["low"])
        | (bars["close"] <= 0)
        | (bars["open"] <= 0)
    ).sum()
    if bad_prices:
        score -= 0.3
        dq.issues.append(f"bad_prices_{int(bad_prices)}")

    dq.score = max(0.0, min(1.0, score))
    dq.grade = get_quality_grade(dq.score)
    return dq


def get_quality_grade(score: float) -> str:
    if score >= 0.9:
        return "A"
    if score >= 0.75:
        return "B"
    if score >= 0.6:
        return "C"
    if score >= 0.4:
        return "D"
    return "F"


def should_trade_symbol(dq: DataQualityScore, min_score: float = 0.6) -> bool:
    """Whether data quality is sufficient to act on signals."""
    return dq.score >= min_score
