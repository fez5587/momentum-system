"""Criteria scoring logic."""

from typing import TYPE_CHECKING
from strategy.models import CriteriaWeights, CriteriaResult

if TYPE_CHECKING:
    pass


def score_criteria(
    results: list[CriteriaResult],
    weights: CriteriaWeights,
) -> tuple[int, int, float]:
    """Score criteria results and return count, total, and percentage."""
    passed = [r for r in results if r.passed]
    weighted_sum = sum(getattr(weights, r.name) for r in passed)
    total = sum(getattr(weights, r.name) for r in results)
    pct = min(100.0, max(0.0, (weighted_sum / total * 100))) if total > 0 else 0.0
    return len(passed), len(results), pct


def build_criteria_result(
    name: str, passed: bool, reason: str | None = None
) -> CriteriaResult:
    """Build a criteria result."""
    return CriteriaResult(name=name, passed=passed, reason=reason)
