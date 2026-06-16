"""Strategy evaluation package.

Implements Ross Cameron style momentum trading strategy:
- Structure detection (bull flags, first pullback, gap & go)
- Key levels tracking (VWAP, premarket high, opening range)
- Setup quality scoring
- Market regime detection
"""

from strategy.evaluation.setup_evaluator import evaluate_setup
from strategy.evaluation.criteria import score_criteria, build_criteria_result
from strategy.evaluation.structure import (
    classify_setup,
    detect_bull_flag,
    detect_first_pullback,
    detect_gap_and_go,
    detect_hod_break,
    StructureDetectionResult,
    SetupType,
)
from strategy.evaluation.levels import (
    compute_key_levels,
    calculate_vwap,
    calculate_ema,
    KeyLevels,
    get_trigger_levels,
    get_stop_levels,
)
from strategy.evaluation.quality import (
    calculate_setup_quality,
    QualityScore,
    QualityThresholds,
)
from strategy.evaluation.regime import (
    assess_market_regime,
    get_regime_adjustment,
    should_trade_in_regime,
    MarketRegime,
    RegimeAssessment,
)
from strategy.evaluation.first_candles import (
    calculate_first_candle_features,
    classify_opening_strength,
    FirstCandleFeatures,
)
from strategy.evaluation.volume_metrics import (
    calculate_enhanced_volume_metrics,
    calculate_time_of_day_rvol,
    calculate_float_rotation,
    VolumeMetrics,
)
from strategy.evaluation.data_quality import (
    calculate_data_quality_score,
    should_trade_symbol,
    get_quality_grade,
    DataQualityScore,
)

__all__ = [
    # Setup evaluation
    "evaluate_setup",
    "score_criteria",
    "build_criteria_result",
    # Structure detection
    "classify_setup",
    "detect_bull_flag",
    "detect_first_pullback",
    "detect_gap_and_go",
    "detect_hod_break",
    "StructureDetectionResult",
    "SetupType",
    # Levels
    "compute_key_levels",
    "calculate_vwap",
    "calculate_ema",
    "KeyLevels",
    "get_trigger_levels",
    "get_stop_levels",
    # Quality
    "calculate_setup_quality",
    "QualityScore",
    "QualityThresholds",
    # Regime
    "assess_market_regime",
    "get_regime_adjustment",
    "should_trade_in_regime",
    "MarketRegime",
    "RegimeAssessment",
    # First candles
    "calculate_first_candle_features",
    "classify_opening_strength",
    "FirstCandleFeatures",
    # Volume metrics
    "calculate_enhanced_volume_metrics",
    "calculate_time_of_day_rvol",
    "calculate_float_rotation",
    "VolumeMetrics",
    # Data quality
    "calculate_data_quality_score",
    "should_trade_symbol",
    "get_quality_grade",
    "DataQualityScore",
]
