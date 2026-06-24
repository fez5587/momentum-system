"""Setup evaluator — the heart of Milestone 1.

Pure function: bars + context in, SetupEvaluationResult (ready/blocked/late) out.
No broker, DB, or UI dependencies.
"""

from __future__ import annotations

from datetime import datetime, time

import pandas as pd

from strategy.models import (
    CriteriaWeights,
    SetupCriteria,
    SetupEvaluationResult,
)
from strategy.evaluation.criteria import build_criteria_result, score_criteria
from strategy.evaluation.levels import compute_key_levels, get_stop_levels
from strategy.evaluation.structure import SetupType, classify_setup
from strategy.evaluation.quality import calculate_setup_quality
from strategy.evaluation.first_candles import calculate_first_candle_features
from strategy.evaluation.volume_metrics import calculate_enhanced_volume_metrics
from strategy.evaluation.data_quality import calculate_data_quality_score
from strategy.risk.entry_cuts import EntryCutoffConfig, check_entry_cutoff

MIN_BARS = 10


def evaluate_setup(
    bars: pd.DataFrame,
    previous_close: float | None = None,
    avg_daily_volume: float | None = None,
    criteria: SetupCriteria | None = None,
    weights: CriteriaWeights | None = None,
    evaluation_time: datetime | None = None,
    entry_cutoff: EntryCutoffConfig | None = None,
    ready_score_pct: float = 60.0,
    min_bars: int = MIN_BARS,
    catalyst_score: float | None = None,
) -> SetupEvaluationResult:
    """Evaluate a symbol's session bars for a momentum setup.

    Status semantics:
      ready   — criteria score >= ready_score_pct, valid structure, before cutoff
      late    — would be ready but the entry cutoff has passed
      blocked — anything else (with the dominant blocking reason)
    """
    criteria = criteria or SetupCriteria()
    weights = weights or CriteriaWeights()
    evaluation_time = evaluation_time or datetime.now()

    if bars is None or bars.empty or len(bars) < min_bars:
        return _blocked(
            reason=f"insufficient_data ({0 if bars is None else len(bars)} bars, need {min_bars})",
            evaluation_time=evaluation_time,
        )

    bars = bars.reset_index(drop=True)
    price = float(bars["close"].iloc[-1])
    open_price = float(bars["open"].iloc[0])
    ref_close = previous_close if previous_close and previous_close > 0 else open_price
    gap_pct = (open_price - ref_close) / ref_close if ref_close > 0 else 0.0
    intraday_change = (price - open_price) / open_price if open_price > 0 else 0.0

    dq = calculate_data_quality_score(bars)
    vol = calculate_enhanced_volume_metrics(bars, avg_daily_volume=avg_daily_volume)
    relative_volume = vol.relative_volume if avg_daily_volume else max(
        1.0, vol.last_bar_volume / vol.avg_bar_volume if vol.avg_bar_volume else 1.0
    )
    levels = compute_key_levels(bars, previous_close=previous_close)
    structure = classify_setup(
        bars,
        premarket_high=levels.premarket_high,
        session_high=levels.high_of_day,
        min_quality_score=0.2,
    )
    first = calculate_first_candle_features(bars, premarket_high=levels.premarket_high)
    above_vwap = levels.vwap is not None and price >= levels.vwap

    results = [
        build_criteria_result(
            "sufficient_data",
            dq.score >= criteria.min_quality_score,
            f"quality {dq.score:.2f} (min {criteria.min_quality_score})",
        ),
        build_criteria_result(
            "gap",
            gap_pct >= criteria.gap_pct_min or intraday_change >= criteria.gap_pct_min,
            f"gap {gap_pct:.1%} / intraday {intraday_change:.1%} (min {criteria.gap_pct_min:.0%})",
        ),
        build_criteria_result(
            "relative_volume",
            relative_volume >= criteria.relative_volume_min,
            f"rvol {relative_volume:.1f} (min {criteria.relative_volume_min})",
        ),
        build_criteria_result(
            "impulse",
            structure.is_valid and structure.setup_type != SetupType.NONE,
            f"structure {structure.setup_type.value}: {structure.reason or 'ok'}",
        ),
        build_criteria_result(
            "pullback",
            structure.setup_type
            in (SetupType.FIRST_PULLBACK, SetupType.BULL_FLAG, SetupType.GAP_AND_GO,
                SetupType.HOD_BREAK, SetupType.CONTINUATION_FALLBACK,
                SetupType.OPENING_RANGE_BREAK)
            and structure.is_valid,
            "pullback/consolidation structure",
        ),
        build_criteria_result(
            "pullback_volume",
            structure.quality_score >= 0.3,
            f"structure quality {structure.quality_score:.2f}",
        ),
        build_criteria_result("vwap", above_vwap, f"price vs VWAP {levels.vwap}"),
        build_criteria_result(
            "candle_quality",
            first.opening_strength != "weak",
            f"opening {first.opening_strength}",
        ),
        build_criteria_result(
            "breakout",
            structure.breakout_level is not None
            and price >= structure.breakout_level * 0.995,
            f"price {price:.2f} vs breakout {structure.breakout_level}",
        ),
    ]

    passed_n, total_n, score_pct = score_criteria(results, weights)
    passed_names = [r.name for r in results if r.passed]
    failed_names = [r.name for r in results if not r.passed]

    quality = calculate_setup_quality(
        gap_pct=max(gap_pct, intraday_change),
        relative_volume=relative_volume,
        structure_quality=structure.quality_score,
        above_vwap=above_vwap,
        opening_strength=first.opening_strength,
        data_quality=dq.score,
        catalyst_score=catalyst_score,
    )

    entry_price = structure.breakout_level or price
    stop_candidates = get_stop_levels(levels, entry_price)
    stop_price = structure.stop_level or (
        stop_candidates[0][1] if stop_candidates else round(entry_price * 0.97, 4)
    )

    setups = []
    if structure.is_valid and structure.setup_type != SetupType.NONE:
        setups.append(
            {
                "setup_type": structure.setup_type.value,
                "entry_price": round(float(entry_price), 4),
                "stop_loss_price": round(float(stop_price), 4),
                "quality_score": quality.score,
                "quality_grade": quality.grade,
                "confidence": round(score_pct / 100.0, 4),
                # surfaced for the live VWAP + anti-chase entry gates + observability
                "vwap": (round(float(levels.vwap), 4)
                         if levels.vwap is not None else None),
                "above_vwap": bool(above_vwap),
                "day_open": round(float(open_price), 4) if open_price > 0 else None,
                "cum_volume": float(bars["volume"].sum()),
            }
        )

    base = dict(
        evaluated_at=evaluation_time.isoformat(),
        price=price,
        gap_pct=round(gap_pct, 6),
        relative_volume=round(relative_volume, 4),
        criteria_passed=passed_n,
        criteria_total=total_n,
        success_score_pct=round(score_pct, 2),
        criteria_names_passed=passed_names,
        criteria_names_failed=failed_names,
        criteria_detail=[
            {"name": r.name, "passed": r.passed, "reason": r.reason} for r in results
        ],
        setups=setups,
    )

    if score_pct < ready_score_pct or not setups:
        reason = (
            f"score {score_pct:.0f}% < {ready_score_pct:.0f}%"
            if score_pct < ready_score_pct
            else "no valid setup structure"
        )
        if failed_names:
            reason += f" (failed: {', '.join(failed_names)})"
        return SetupEvaluationResult(status="blocked", reason=reason, **base)

    cutoff = check_entry_cutoff(
        trigger_time=time(evaluation_time.hour, evaluation_time.minute),
        cutoff_config=entry_cutoff or EntryCutoffConfig(),
    )
    if not cutoff.passed:
        return SetupEvaluationResult(
            status="late", reason=cutoff.reason or "past entry cutoff", **base
        )

    return SetupEvaluationResult(status="ready", reason=None, **base)


def _blocked(reason: str, evaluation_time: datetime) -> SetupEvaluationResult:
    return SetupEvaluationResult(
        status="blocked",
        reason=reason,
        evaluated_at=evaluation_time.isoformat(),
        price=0.0,
        gap_pct=0.0,
        relative_volume=0.0,
        criteria_passed=0,
        criteria_total=0,
        success_score_pct=0.0,
    )
