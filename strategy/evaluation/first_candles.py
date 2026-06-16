"""First-candle features: opening drive strength classification."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class FirstCandleFeatures:
    """Features of the first regular-hours candles."""

    first_candle_range_pct: float = 0.0
    first_candle_body_pct: float = 0.0
    first_candle_green: bool = False
    first_candle_volume: int = 0
    open_above_premarket_high: bool = False
    opening_strength: str = "neutral"


def calculate_first_candle_features(
    bars: pd.DataFrame,
    premarket_high: float | None = None,
    n_candles: int = 1,
) -> FirstCandleFeatures:
    """Compute features from the first regular-hours candle(s)."""
    feats = FirstCandleFeatures()
    if bars.empty:
        return feats

    if "is_regular_hours" in bars.columns:
        rth = bars[bars["is_regular_hours"].astype(bool)]
    else:
        rth = bars
    if rth.empty:
        return feats

    first = rth.head(max(1, n_candles))
    o = float(first["open"].iloc[0])
    c = float(first["close"].iloc[-1])
    h = float(first["high"].max())
    lo = float(first["low"].min())
    if o > 0:
        feats.first_candle_range_pct = (h - lo) / o
        feats.first_candle_body_pct = abs(c - o) / o
    feats.first_candle_green = c > o
    feats.first_candle_volume = int(first["volume"].sum())
    if premarket_high is not None:
        feats.open_above_premarket_high = o > premarket_high
    feats.opening_strength = classify_opening_strength(feats)
    return feats


def classify_opening_strength(feats: FirstCandleFeatures) -> str:
    """Classify opening strength as strong / neutral / weak."""
    if feats.first_candle_green and feats.first_candle_body_pct >= 0.02:
        return "strong"
    if not feats.first_candle_green and feats.first_candle_body_pct >= 0.02:
        return "weak"
    return "neutral"
