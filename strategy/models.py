"""Strategy data models.

Pydantic models for configuration and results.
"""

from pydantic import BaseModel, Field


class SetupCriteria(BaseModel):
    """Configuration for setup evaluation criteria."""

    gap_pct_min: float = Field(default=0.05, description="Minimum gap percentage")
    relative_volume_min: float = Field(
        default=2.5, description="Minimum relative volume"
    )
    impulse_size_min: float = Field(default=0.05, description="Minimum impulse size")
    max_pullback_depth_pct: float = Field(
        default=0.40, description="Max pullback depth percent"
    )
    pullback_volume_ratio_max: float = Field(
        default=0.6, description="Max pullback volume ratio"
    )
    breakout_volume_ratio_min: float = Field(
        default=1.5, description="Min breakout volume ratio"
    )
    min_quality_score: float = Field(
        default=0.60, description="Minimum data quality score (default: 0.60)"
    )


class CriteriaWeights(BaseModel):
    """Weights for scoring criteria."""

    sufficient_data: int = 10
    gap: int = 15
    relative_volume: int = 15
    impulse: int = 15
    pullback: int = 10
    pullback_volume: int = 10
    vwap: int = 10
    candle_quality: int = 5
    breakout: int = 10


class CriteriaResult(BaseModel):
    """Result of criteria evaluation."""

    name: str
    passed: bool
    reason: str | None = None


class SetupEvaluationResult(BaseModel):
    """Result of setup evaluation."""

    status: str = Field(description="Status: ready, blocked, or late")
    reason: str | None = Field(
        default=None, description="Blocking reason if status is blocked/late"
    )
    evaluated_at: str | None = Field(default=None, description="Evaluation timestamp")
    price: float = Field(description="Current price")
    gap_pct: float = Field(description="Gap percentage")
    relative_volume: float = Field(description="Relative volume")
    criteria_passed: int = Field(description="Number of criteria passed")
    criteria_total: int = Field(description="Total number of criteria")
    success_score_pct: float = Field(description="Success score percentage")
    criteria_names_passed: list[str] = Field(
        default_factory=list, description="Names of passed criteria"
    )
    criteria_names_failed: list[str] = Field(
        default_factory=list, description="Names of failed criteria"
    )
    setups: list[dict] = Field(default_factory=list, description="Detected setups")
    criteria_detail: list[dict] = Field(
        default_factory=list,
        description="Per-criterion breakdown: {name, passed, reason}",
    )
