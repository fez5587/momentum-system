"""Market regime assessment (hot / normal / cold tape)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MarketRegime(str, Enum):
    HOT = "hot"
    NORMAL = "normal"
    COLD = "cold"


@dataclass
class RegimeAssessment:
    regime: MarketRegime
    score: float
    reason: str


def assess_market_regime(
    gappers_count: int = 0,
    avg_gap_pct: float = 0.0,
    spy_change_pct: float = 0.0,
) -> RegimeAssessment:
    """Assess the tape from breadth of gappers and index behavior."""
    score = 0.0
    score += min(1.0, gappers_count / 10.0) * 0.5
    score += min(1.0, max(0.0, avg_gap_pct / 0.15)) * 0.3
    score += min(1.0, max(0.0, (spy_change_pct + 0.01) / 0.02)) * 0.2

    if score >= 0.65:
        regime = MarketRegime.HOT
        reason = f"{gappers_count} gappers, avg gap {avg_gap_pct:.1%}"
    elif score >= 0.35:
        regime = MarketRegime.NORMAL
        reason = "moderate breadth"
    else:
        regime = MarketRegime.COLD
        reason = "few gappers / weak tape"
    return RegimeAssessment(regime=regime, score=round(score, 3), reason=reason)


def get_regime_adjustment(regime: MarketRegime) -> float:
    """Position-size multiplier for the regime."""
    return {MarketRegime.HOT: 1.0, MarketRegime.NORMAL: 0.75, MarketRegime.COLD: 0.5}[
        regime
    ]


def should_trade_in_regime(regime: MarketRegime, allow_cold: bool = False) -> bool:
    if regime == MarketRegime.COLD:
        return allow_cold
    return True
