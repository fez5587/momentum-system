"""Setup quality scoring."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class QualityThresholds:
    """Thresholds used to grade setup quality."""

    a_grade: float = 0.80
    b_grade: float = 0.65
    c_grade: float = 0.50
    min_tradeable: float = 0.50


@dataclass
class QualityScore:
    """Composite quality score for a detected setup."""

    score: float = 0.0
    grade: str = "F"
    components: dict = field(default_factory=dict)
    tradeable: bool = False


def calculate_setup_quality(
    gap_pct: float,
    relative_volume: float,
    structure_quality: float,
    above_vwap: bool,
    opening_strength: str = "neutral",
    data_quality: float = 1.0,
    thresholds: QualityThresholds | None = None,
) -> QualityScore:
    """Blend setup ingredients into one 0..1 quality score.

    Weights: gap 20%, RVOL 25%, structure 30%, VWAP 10%,
    opening strength 5%, data quality 10%.
    """
    thresholds = thresholds or QualityThresholds()

    gap_component = min(1.0, max(0.0, gap_pct / 0.10))
    rvol_component = min(1.0, max(0.0, relative_volume / 5.0))
    structure_component = min(1.0, max(0.0, structure_quality))
    vwap_component = 1.0 if above_vwap else 0.0
    opening_component = {"strong": 1.0, "neutral": 0.5, "weak": 0.0}.get(
        opening_strength, 0.5
    )
    dq_component = min(1.0, max(0.0, data_quality))

    score = (
        0.20 * gap_component
        + 0.25 * rvol_component
        + 0.30 * structure_component
        + 0.10 * vwap_component
        + 0.05 * opening_component
        + 0.10 * dq_component
    )

    if score >= thresholds.a_grade:
        grade = "A"
    elif score >= thresholds.b_grade:
        grade = "B"
    elif score >= thresholds.c_grade:
        grade = "C"
    else:
        grade = "F"

    return QualityScore(
        score=round(score, 4),
        grade=grade,
        components={
            "gap": gap_component,
            "relative_volume": rvol_component,
            "structure": structure_component,
            "vwap": vwap_component,
            "opening_strength": opening_component,
            "data_quality": dq_component,
        },
        tradeable=score >= thresholds.min_tradeable,
    )
