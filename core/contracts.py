"""Module contracts and interfaces for the momentum trading system.

Every module must implement the ModuleContract protocol, defining:
- input_schema: What the module expects
- output_schema: What the module produces
- run(): Execute the module
- validate_input(): Ensure data meets contracts
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Protocol, TypeVar

T_Input = TypeVar("T_Input")
T_Output = TypeVar("T_Output")


@dataclass
class ModuleMetrics:
    """Standard metrics every module should track."""

    module_name: str
    timestamp: datetime
    duration_ms: float
    input_count: int = 0
    output_count: int = 0
    error_count: int = 0
    warnings: list[str] = field(default_factory=list)
    custom_metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "module_name": self.module_name,
            "timestamp": self.timestamp.isoformat(),
            "duration_ms": self.duration_ms,
            "input_count": self.input_count,
            "output_count": self.output_count,
            "error_count": self.error_count,
            "warnings": self.warnings,
            "custom_metrics": self.custom_metrics,
        }


@dataclass
class ValidationFailure:
    """Single validation failure with context."""

    field: str
    value: Any
    reason: str
    severity: str = "error"  # "error" or "warning"


@dataclass
class ModuleResult:
    """Base class for all module results."""

    success: bool
    timestamp: datetime
    metrics: ModuleMetrics
    validation_failures: list[ValidationFailure] = field(default_factory=list)

    def has_errors(self) -> bool:
        return bool(self.validation_failures or not self.success)

    def error_summary(self) -> str:
        """Human-readable error summary."""
        if not self.validation_failures:
            return "No errors"
        errors = [f for f in self.validation_failures if f.severity == "error"]
        if not errors:
            return f"{len(self.validation_failures)} warnings only"
        return f"{len(errors)} error(s), {len([f for f in self.validation_failures if f.severity == 'warning'])} warning(s)"


class ModuleContract(Protocol):
    """Contract every module must fulfill."""

    def run(self, input: Any) -> ModuleResult:
        """Execute the module.

        Args:
            input: Module-specific input conforming to input_schema

        Returns:
            Module-specific result inheriting from ModuleResult
        """
        ...

    def validate_input(self, input: Any) -> tuple[bool, list[ValidationFailure]]:
        """Validate input before processing.

        Args:
            input: Data to validate

        Returns:
            (is_valid, failures)
        """
        ...

    def get_metrics(self) -> ModuleMetrics:
        """Get last execution metrics."""
        ...


# ============================================================================
# INGESTION MODULE CONTRACTS
# ============================================================================


@dataclass
class BarIngestRequest:
    """Input contract for ingestion module."""

    symbols: list[str]
    session_date: date
    lookback_minutes: int = 120  # how many minutes of history to fetch
    source: str = "alpaca"  # "alpaca", "test", "file"


@dataclass
class BarIngestResult(ModuleResult):
    """Output contract for ingestion module."""

    minute_rows_inserted: int = 0
    symbols_updated: list[str] = field(default_factory=list)
    source_health: dict[str, str] = field(default_factory=dict)  # {source: "healthy"|"error"}


# ============================================================================
# RESEARCH MODULE CONTRACTS
# ============================================================================


@dataclass
class ResearchRequest:
    """Input contract for research module."""

    symbols: list[str]
    session_date: date
    lookback_days: int = 30  # for daily history


@dataclass
class ResearchResult(ModuleResult):
    """Output contract for research module."""

    symbols_researched: list[str] = field(default_factory=list)
    symbols_with_data: list[str] = field(default_factory=list)
    symbols_missing_data: list[str] = field(default_factory=list)
    total_bars_available: int = 0


# ============================================================================
# EVALUATION MODULE CONTRACTS
# ============================================================================


@dataclass
class CriterionDetail:
    """Result of evaluating one criterion."""

    name: str
    passed: bool
    value: float  # actual metric value
    threshold: float | None = None  # minimum to pass
    reason: str = ""
    confidence: float = 1.0  # 0-1, how confident in this result


@dataclass
class EvaluationRequest:
    """Input contract for evaluation module."""

    symbol: str
    bars_df: Any  # pd.DataFrame with {timestamp, open, high, low, close, volume}
    previous_close: float
    avg_daily_volume: float
    session_date: date


@dataclass
class EvaluationResult(ModuleResult):
    """Output contract for evaluation module."""

    symbol: str = ""
    status: str = "late"  # "ready", "blocked", "late", "error"
    score: float = 0.0  # 0-100
    criteria: list[CriterionDetail] = field(default_factory=list)
    entry_price: float | None = None
    stop_loss: float | None = None
    target_price: float | None = None
    quality_score: float = 0.0  # 0-1
    blocking_reason: str | None = None
    metrics_dict: dict[str, float] = field(default_factory=dict)  # gap%, rvol, etc.


# ============================================================================
# EXECUTION MODULE CONTRACTS
# ============================================================================


@dataclass
class ExecutionRequest:
    """Input contract for execution module."""

    symbol: str
    entry_price: float
    stop_price: float
    target_price: float
    score: float
    criteria_count: int
    account_equity: float
    current_positions: int
    today_realized_pnl: float


@dataclass
class ExecutionResult(ModuleResult):
    """Output contract for execution module."""

    symbol: str = ""
    status: str = ""  # "approved", "rejected", "pending_approval", "error"
    reason: str = ""  # why it was approved/rejected
    order_id: str | None = None
    shares: int | None = None
    risk_dollars: float | None = None
    risk_checks_applied: list[str] = field(default_factory=list)


# ============================================================================
# BROKER MODULE CONTRACTS
# ============================================================================


@dataclass
class BrokerRequest:
    """Input contract for broker module."""

    symbol: str
    order_id: str
    entry_price: float
    stop_price: float
    target_price: float
    shares: int
    order_type: str = "limit"  # "limit" or "market"


@dataclass
class BrokerResult(ModuleResult):
    """Output contract for broker module."""

    symbol: str = ""
    status: str = ""  # "submitted", "rejected", "error"
    broker_order_id: str | None = None
    submitted_at: datetime | None = None
