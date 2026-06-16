"""Market structure detection for momentum setups.

Implements bull flag, first pullback, and gap-and-go pattern detection
based on Ross Cameron momentum strategy specifications.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Literal

import numpy as np
import pandas as pd


class SetupType(Enum):
    """Types of momentum setups."""

    GAP_AND_GO = "gap_and_go"
    OPENING_RANGE_BREAK = "opening_range_break"
    FIRST_PULLBACK = "first_pullback"
    BULL_FLAG = "bull_flag"
    HOD_BREAK = "hod_break"
    CONTINUATION_FALLBACK = "continuation_fallback"
    NONE = "none"


@dataclass
class StructureDetectionResult:
    """Result of structure detection."""

    setup_type: SetupType
    is_valid: bool
    impulse_start_idx: int | None = None
    impulse_end_idx: int | None = None
    consolidation_start_idx: int | None = None
    consolidation_end_idx: int | None = None
    breakout_level: float | None = None
    stop_level: float | None = None
    quality_score: float = 0.0
    reason: str | None = None


def detect_bull_flag(
    bars: pd.DataFrame,
    min_impulse_bars: int = 3,
    max_consolidation_bars: int = 10,
    min_impulse_pct: float = 0.03,
) -> StructureDetectionResult:
    """Detect bull flag pattern.

    Pattern: Strong impulse leg up, followed by consolidation
    with contracting range, then breakout.

    Args:
        bars: OHLCV DataFrame
        min_impulse_bars: Minimum bars for impulse leg
        max_consolidation_bars: Maximum bars for consolidation
        min_impulse_pct: Minimum impulse percentage

    Returns:
        StructureDetectionResult with pattern details
    """
    if len(bars) < min_impulse_bars + 2:
        return StructureDetectionResult(
            setup_type=SetupType.BULL_FLAG,
            is_valid=False,
            reason="Insufficient bars for pattern detection",
        )

    highs = bars["high"].values
    lows = bars["low"].values
    closes = bars["close"].values

    # Find impulse leg (strong upward movement)
    impulse_end = None
    for i in range(min_impulse_bars, len(bars)):
        # Calculate gain over last N bars
        gain = (closes[i] - closes[i - min_impulse_bars]) / closes[i - min_impulse_bars]
        if gain >= min_impulse_pct:
            impulse_end = i
            break

    if impulse_end is None:
        return StructureDetectionResult(
            setup_type=SetupType.BULL_FLAG,
            is_valid=False,
            reason="No impulse leg found",
        )

    impulse_start = max(0, impulse_end - min_impulse_bars)

    # Look for consolidation after impulse
    consolidation_start = impulse_end + 1
    if consolidation_start >= len(bars):
        return StructureDetectionResult(
            setup_type=SetupType.BULL_FLAG,
            is_valid=False,
            reason="No consolidation phase after impulse",
        )

    # Find consolidation end (contracting range)
    consolidation_end = consolidation_start
    max_consolidation_idx = min(
        consolidation_start + max_consolidation_bars, len(bars) - 1
    )

    prev_range = highs[impulse_end] - lows[impulse_end]
    for i in range(consolidation_start, max_consolidation_idx):
        current_range = highs[i] - lows[i]
        # Range should be contracting or stable
        if current_range <= prev_range * 1.2:  # Allow 20% expansion
            consolidation_end = i
            prev_range = min(prev_range, current_range)
        else:
            break

    consolidation_bars = consolidation_end - consolidation_start + 1
    if consolidation_bars < 2:
        return StructureDetectionResult(
            setup_type=SetupType.BULL_FLAG,
            is_valid=False,
            reason="Consolidation too short",
        )

    # Calculate quality score
    impulse_pct = (closes[impulse_end] - closes[impulse_start]) / closes[impulse_start]
    consolidation_depth = (
        highs[consolidation_start : consolidation_end + 1].max()
        - lows[consolidation_start : consolidation_end + 1].min()
    ) / closes[impulse_end]

    # Quality factors
    impulse_quality = min(1.0, impulse_pct / 0.10)  # Max at 10% impulse
    consolidation_quality = max(0, 1.0 - (consolidation_bars / max_consolidation_bars))
    depth_quality = max(
        0, 1.0 - (consolidation_depth / 0.05)
    )  # Penalty for deep pullback

    quality_score = (
        impulse_quality * 0.4 + consolidation_quality * 0.3 + depth_quality * 0.3
    )

    # Breakout level is the high of the consolidation
    breakout_level = highs[consolidation_start : consolidation_end + 1].max()

    # Stop level is the low of the consolidation or impulse
    stop_level = min(
        lows[impulse_start : consolidation_end + 1].min(),
        lows[consolidation_start] * 0.995,
    )

    return StructureDetectionResult(
        setup_type=SetupType.BULL_FLAG,
        is_valid=True,
        impulse_start_idx=impulse_start,
        impulse_end_idx=impulse_end,
        consolidation_start_idx=consolidation_start,
        consolidation_end_idx=consolidation_end,
        breakout_level=float(breakout_level),
        stop_level=float(stop_level),
        quality_score=float(quality_score),
        reason=None,
    )


def detect_first_pullback(
    bars: pd.DataFrame,
    max_pullback_bars: int = 5,
    max_pullback_depth_pct: float = 0.40,
    impulse_search_bars: int = 120,
) -> StructureDetectionResult:
    """Detect first pullback after impulse.

    Pattern: Strong move up, shallow pullback (not more than 40% of impulse),
    then continuation.

    Args:
        bars: OHLCV DataFrame
        max_pullback_bars: Maximum bars for pullback
        max_pullback_depth_pct: Maximum pullback as % of impulse

    Returns:
        StructureDetectionResult with pattern details
    """
    if len(bars) < 5:
        return StructureDetectionResult(
            setup_type=SetupType.FIRST_PULLBACK,
            is_valid=False,
            reason="Insufficient bars",
        )

    highs = bars["high"].values
    lows = bars["low"].values
    closes = bars["close"].values

    # Find impulse high in early session window (avoid late-day highs)
    search_end = min(len(highs), impulse_search_bars)
    if search_end < 5:
        return StructureDetectionResult(
            setup_type=SetupType.FIRST_PULLBACK,
            is_valid=False,
            reason="Insufficient early-session bars",
        )

    impulse_high_idx = int(np.argmax(highs[:search_end]))
    impulse_high = highs[impulse_high_idx]

    # Calculate impulse from recent low
    lookback = min(impulse_high_idx, 10)
    if lookback < 3:
        return StructureDetectionResult(
            setup_type=SetupType.FIRST_PULLBACK,
            is_valid=False,
            reason="Impulse too short",
        )

    impulse_low = lows[impulse_high_idx - lookback : impulse_high_idx].min()
    impulse_range = impulse_high - impulse_low

    if impulse_range / impulse_low < 0.03:  # Need at least 3% impulse
        return StructureDetectionResult(
            setup_type=SetupType.FIRST_PULLBACK,
            is_valid=False,
            reason="Impulse too small",
        )

    # Look for pullback after impulse
    if impulse_high_idx >= len(bars) - 1:
        return StructureDetectionResult(
            setup_type=SetupType.FIRST_PULLBACK,
            is_valid=False,
            reason="No pullback yet",
        )

    pullback_start = impulse_high_idx + 1
    pullback_end = min(pullback_start + max_pullback_bars - 1, len(bars) - 1)

    pullback_low = lows[pullback_start : pullback_end + 1].min()
    pullback_depth = impulse_high - pullback_low
    pullback_pct = pullback_depth / impulse_range

    if pullback_pct > max_pullback_depth_pct:
        return StructureDetectionResult(
            setup_type=SetupType.FIRST_PULLBACK,
            is_valid=False,
            reason=f"Pullback too deep: {pullback_pct:.1%}",
        )

    # Quality scoring
    depth_quality = max(0, 1.0 - (pullback_pct / max_pullback_depth_pct))
    pullback_bars = pullback_end - pullback_start + 1
    speed_quality = max(0, 1.0 - (pullback_bars / max_pullback_bars))

    quality_score = depth_quality * 0.6 + speed_quality * 0.4

    # Entry on reclaim of pullback high
    breakout_level = impulse_high
    stop_level = pullback_low * 0.995

    return StructureDetectionResult(
        setup_type=SetupType.FIRST_PULLBACK,
        is_valid=True,
        impulse_start_idx=impulse_high_idx - lookback,
        impulse_end_idx=impulse_high_idx,
        consolidation_start_idx=pullback_start,
        consolidation_end_idx=pullback_end,
        breakout_level=float(breakout_level),
        stop_level=float(stop_level),
        quality_score=float(quality_score),
        reason=None,
    )


def detect_gap_and_go(
    bars: pd.DataFrame,
    premarket_high: float | None = None,
    min_gap_pct: float = 0.03,
) -> StructureDetectionResult:
    """Detect gap-and-go pattern.

    Pattern: Stock gaps up, opens strong, and continues higher
    breaking premarket or opening range highs.

    Args:
        bars: OHLCV DataFrame (including premarket)
        premarket_high: High of premarket session
        min_gap_pct: Minimum gap percentage

    Returns:
        StructureDetectionResult with pattern details
    """
    if len(bars) < 5:
        return StructureDetectionResult(
            setup_type=SetupType.GAP_AND_GO, is_valid=False, reason="Insufficient bars"
        )

    opens = bars["open"].values
    highs = bars["high"].values
    closes = bars["close"].values

    # Check for gap (first bar open vs some reference)
    # For simplicity, compare first bar open to recent lows
    reference_price = bars["low"].iloc[: min(5, len(bars))].min()
    gap_pct = (opens[0] - reference_price) / reference_price

    if gap_pct < min_gap_pct:
        return StructureDetectionResult(
            setup_type=SetupType.GAP_AND_GO,
            is_valid=False,
            reason=f"Gap too small: {gap_pct:.1%}",
        )

    # Determine breakout level
    if premarket_high:
        breakout_level = premarket_high
    else:
        # Use first few bars high
        breakout_level = highs[: min(3, len(bars))].max()

    # Check if we've broken out
    current_price = closes[-1]
    if current_price < breakout_level * 0.97:
        return StructureDetectionResult(
            setup_type=SetupType.GAP_AND_GO,
            is_valid=False,
            reason="Waiting for breakout confirmation",
        )

    # Quality based on gap size and trend strength
    gap_quality = min(1.0, gap_pct / 0.15)  # Max at 15% gap

    # Check if price is holding above VWAP or continuing up
    recent_trend = (closes[-1] - closes[min(len(closes) - 1, 3)]) / closes[
        min(len(closes) - 1, 3)
    ]
    trend_quality = 1.0 if recent_trend > 0 else 0.5

    quality_score = gap_quality * 0.6 + trend_quality * 0.4

    # Stop below recent pullback or opening range
    stop_level = bars["low"].iloc[: min(10, len(bars))].min() * 0.995

    return StructureDetectionResult(
        setup_type=SetupType.GAP_AND_GO,
        is_valid=True,
        breakout_level=float(breakout_level),
        stop_level=float(stop_level),
        quality_score=float(quality_score),
        reason=None,
    )


def detect_hod_break(
    bars: pd.DataFrame,
    session_high: float | None = None,
) -> StructureDetectionResult:
    """Detect high-of-day breakout.

    Pattern: Stock consolidates under the HOD, then breaks through
    with volume confirmation.

    Args:
        bars: OHLCV DataFrame
        session_high: Current session high (if known)

    Returns:
        StructureDetectionResult with pattern details
    """
    if len(bars) < 10:
        return StructureDetectionResult(
            setup_type=SetupType.HOD_BREAK, is_valid=False, reason="Insufficient bars"
        )

    highs = bars["high"].values
    closes = bars["close"].values

    # Determine HOD
    if session_high:
        hod = session_high
    else:
        hod = highs.max()

    # Find consolidation under HOD
    hod_idx = np.where(highs == hod)[0][0] if hod in highs else -1

    if hod_idx < 0 or hod_idx < len(bars) - 5:
        return StructureDetectionResult(
            setup_type=SetupType.HOD_BREAK,
            is_valid=False,
            reason="No recent HOD to break",
        )

    # Look for consolidation before HOD
    consolidation_start = max(0, hod_idx - 10)
    consolidation_zone = highs[consolidation_start:hod_idx]

    # Quality: More touches of the level = better setup
    touches = np.sum(consolidation_zone > hod * 0.995)
    consolidation_quality = min(1.0, touches / 3)  # Max at 3 touches

    # Check if we broke and holding
    current_price = closes[-1]
    if current_price < hod * 0.99:
        return StructureDetectionResult(
            setup_type=SetupType.HOD_BREAK,
            is_valid=False,
            reason="Waiting for HOD break",
        )

    quality_score = consolidation_quality * 0.7 + 0.3  # Base score for breaking

    stop_level = bars["low"].iloc[max(0, len(bars) - 5) :].min() * 0.995

    return StructureDetectionResult(
        setup_type=SetupType.HOD_BREAK,
        is_valid=True,
        breakout_level=float(hod),
        stop_level=float(stop_level),
        quality_score=float(quality_score),
        reason=None,
    )


def detect_continuation_fallback(
    bars: pd.DataFrame,
    min_bars: int = 40,
    impulse_window: int = 25,
    min_impulse_pct: float = 0.015,
    max_pullback_pct: float = 0.65,
) -> StructureDetectionResult:
    if len(bars) < min_bars:
        return StructureDetectionResult(
            setup_type=SetupType.CONTINUATION_FALLBACK,
            is_valid=False,
            reason="Insufficient bars for fallback detection",
        )

    highs = bars["high"].values
    lows = bars["low"].values
    closes = bars["close"].values

    search_end = min(len(closes), max(min_bars, 120))
    if search_end < impulse_window + 5:
        return StructureDetectionResult(
            setup_type=SetupType.CONTINUATION_FALLBACK,
            is_valid=False,
            reason="Insufficient early-session window",
        )

    best_start = 0
    best_end = impulse_window
    best_gain = -1.0
    for i in range(0, search_end - impulse_window):
        j = i + impulse_window
        start = closes[i]
        end = closes[j]
        if start <= 0:
            continue
        gain = (end - start) / start
        if gain > best_gain:
            best_gain = gain
            best_start = i
            best_end = j

    if best_gain < min_impulse_pct:
        return StructureDetectionResult(
            setup_type=SetupType.CONTINUATION_FALLBACK,
            is_valid=False,
            reason="No sufficient impulse for fallback",
        )

    impulse_high = highs[best_start : best_end + 1].max()
    impulse_low = lows[best_start : best_end + 1].min()
    impulse_range = impulse_high - impulse_low
    if impulse_range <= 0:
        return StructureDetectionResult(
            setup_type=SetupType.CONTINUATION_FALLBACK,
            is_valid=False,
            reason="Invalid impulse range",
        )

    pullback_start = min(best_end + 1, len(bars) - 1)
    pullback_end = min(pullback_start + 20, len(bars) - 1)
    if pullback_end <= pullback_start:
        return StructureDetectionResult(
            setup_type=SetupType.CONTINUATION_FALLBACK,
            is_valid=False,
            reason="No pullback window",
        )

    pullback_low = lows[pullback_start : pullback_end + 1].min()
    pullback_depth = max(0.0, impulse_high - pullback_low)
    pullback_pct = pullback_depth / impulse_range

    if pullback_pct > max_pullback_pct:
        return StructureDetectionResult(
            setup_type=SetupType.CONTINUATION_FALLBACK,
            is_valid=False,
            reason=f"Fallback pullback too deep: {pullback_pct:.1%}",
        )

    breakout_level = float(impulse_high)
    stop_level = float(pullback_low * 0.995)
    if stop_level >= breakout_level:
        return StructureDetectionResult(
            setup_type=SetupType.CONTINUATION_FALLBACK,
            is_valid=False,
            reason="Invalid fallback risk geometry",
        )

    impulse_quality = min(1.0, best_gain / 0.05)
    pullback_quality = max(0.0, 1.0 - (pullback_pct / max_pullback_pct))
    current_price = closes[-1]
    proximity = max(0.0, 1.0 - abs((breakout_level - current_price) / breakout_level))
    quality_score = float(
        impulse_quality * 0.45 + pullback_quality * 0.35 + proximity * 0.20
    )

    return StructureDetectionResult(
        setup_type=SetupType.CONTINUATION_FALLBACK,
        is_valid=True,
        impulse_start_idx=int(best_start),
        impulse_end_idx=int(best_end),
        consolidation_start_idx=int(pullback_start),
        consolidation_end_idx=int(pullback_end),
        breakout_level=breakout_level,
        stop_level=stop_level,
        quality_score=quality_score,
        reason=None,
    )


def detect_opening_range_breakout(
    bars: pd.DataFrame,
    orb_bars: int = 5,
    min_range_pct: float = 0.004,
    min_break_pct: float = 0.0,
) -> StructureDetectionResult:
    """Opening-range breakout — the EARLY trigger.

    Fires minutes after the open instead of waiting for a pullback to form: the
    opening range is the high/low of the first ``orb_bars`` regular-hours bars
    (09:30-09:3x); a later bar closing above that high, on at least opening-range
    average volume, is the breakout. Entry = OR high, stop = OR low.
    """
    if "is_regular_hours" in bars.columns:
        rth = bars[bars["is_regular_hours"] == True]  # noqa: E712
        if rth.empty:
            return StructureDetectionResult(
                SetupType.OPENING_RANGE_BREAK, False, reason="no regular-hours bars")
        rth = rth.reset_index(drop=True)
    else:
        rth = bars.reset_index(drop=True)

    if len(rth) < orb_bars + 1:
        return StructureDetectionResult(
            SetupType.OPENING_RANGE_BREAK, False, reason="opening range not complete")

    orb = rth.iloc[:orb_bars]
    orb_high = float(orb["high"].max())
    orb_low = float(orb["low"].min())
    if orb_high <= 0 or (orb_high - orb_low) / orb_high < min_range_pct:
        return StructureDetectionResult(
            SetupType.OPENING_RANGE_BREAK, False, reason="opening range too tight")

    after = rth.iloc[orb_bars:]
    last_close = float(after["close"].iloc[-1])
    if last_close < orb_high * (1.0 + min_break_pct):
        return StructureDetectionResult(
            SetupType.OPENING_RANGE_BREAK, False, reason="no opening-range break yet")

    orb_vol = float(orb["volume"].mean()) if "volume" in orb.columns else 0.0
    break_vol = float(after["volume"].iloc[-1]) if "volume" in after.columns else 0.0
    vol_ok = orb_vol <= 0 or break_vol >= 0.8 * orb_vol

    stop = orb_low * 0.995
    if stop >= orb_high:
        return StructureDetectionResult(
            SetupType.OPENING_RANGE_BREAK, False, reason="invalid ORB risk geometry")

    extension = (last_close - orb_high) / orb_high
    quality = 0.4 + min(0.4, extension / 0.02 * 0.4) + (0.2 if vol_ok else 0.0)
    return StructureDetectionResult(
        setup_type=SetupType.OPENING_RANGE_BREAK,
        is_valid=True,
        breakout_level=orb_high,
        stop_level=float(stop),
        quality_score=float(max(0.0, min(1.0, quality))),
        reason=None,
    )


def classify_setup(
    bars: pd.DataFrame,
    premarket_high: float | None = None,
    session_high: float | None = None,
    min_quality_score: float = 0.2,
) -> StructureDetectionResult:
    """Classify which setup type is present.

    Tests all setup types and returns the best valid one.

    Args:
        bars: OHLCV DataFrame
        premarket_high: High of premarket session
        session_high: Current session high
        min_quality_score: Minimum quality to accept setup

    Returns:
        StructureDetectionResult for best valid setup
    """
    detectors = [
        detect_opening_range_breakout(bars),
        detect_bull_flag(bars, min_impulse_pct=0.015),
        detect_first_pullback(bars, max_pullback_depth_pct=0.6),
        detect_gap_and_go(bars, premarket_high, min_gap_pct=0.02),
        detect_hod_break(bars, session_high),
        detect_continuation_fallback(bars),
    ]

    # Filter valid setups meeting quality threshold
    valid_setups = [
        s for s in detectors if s.is_valid and s.quality_score >= min_quality_score
    ]

    if not valid_setups:
        return StructureDetectionResult(
            setup_type=SetupType.NONE, is_valid=False, reason="No valid setup detected"
        )

    # Return highest quality setup
    return max(valid_setups, key=lambda s: s.quality_score)
