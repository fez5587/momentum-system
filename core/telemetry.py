"""Telemetry collection and event emission for module observability."""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from core.contracts import ModuleMetrics

logger = logging.getLogger(__name__)


@dataclass
class TelemetryCollector:
    """Collect metrics during module execution."""

    module_name: str
    start_time: float = field(default_factory=time.time)
    input_count: int = 0
    output_count: int = 0
    error_count: int = 0
    warnings: list[str] = field(default_factory=list)
    custom_metrics: dict[str, Any] = field(default_factory=dict)

    def record_input(self, count: int = 1) -> None:
        """Record input items processed."""
        self.input_count += count

    def record_output(self, count: int = 1) -> None:
        """Record output items produced."""
        self.output_count += count

    def record_error(self) -> None:
        """Record an error occurred."""
        self.error_count += 1

    def record_warning(self, message: str) -> None:
        """Record a warning."""
        self.warnings.append(message)

    def record_metric(self, name: str, value: float | int | str) -> None:
        """Record a custom metric."""
        self.custom_metrics[name] = value

    def finalize(self) -> ModuleMetrics:
        """Finalize and return metrics."""
        duration_ms = (time.time() - self.start_time) * 1000
        return ModuleMetrics(
            module_name=self.module_name,
            timestamp=datetime.now(),
            duration_ms=duration_ms,
            input_count=self.input_count,
            output_count=self.output_count,
            error_count=self.error_count,
            warnings=self.warnings,
            custom_metrics=self.custom_metrics,
        )


class TelemetryEmitter:
    """Emit telemetry events to event store and observability systems."""

    def __init__(self, event_store=None):
        """Initialize emitter.

        Args:
            event_store: Optional EventStore for persisting telemetry events
        """
        self.event_store = event_store
        self.callbacks: list[Callable[[dict], None]] = []

    def add_callback(self, callback: Callable[[dict], None]) -> None:
        """Add a callback to receive telemetry events."""
        self.callbacks.append(callback)

    def emit_module_tick(
        self,
        module_name: str,
        stage: str,
        metrics: ModuleMetrics | None = None,
        errors: list[dict] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Emit a module tick event.

        Args:
            module_name: Name of module (e.g., "ingestion", "evaluation")
            stage: Stage name (e.g., "started", "processing", "completed")
            metrics: Optional ModuleMetrics object
            errors: Optional list of error details
            correlation_id: Correlation ID linking to signal_ready event
        """
        event = {
            "type": "module_tick",
            "timestamp": datetime.now().isoformat(),
            "module": module_name,
            "stage": stage,
            "metrics": metrics.to_dict() if metrics else None,
            "errors": errors or [],
            "correlation_id": correlation_id,
        }
        self._emit(event)

    def emit_data_validation(
        self,
        module_name: str,
        validation_type: str,
        valid: bool,
        details: dict,
        correlation_id: str | None = None,
    ) -> None:
        """Emit a data validation event.

        Args:
            module_name: Name of module performing validation
            validation_type: Type of validation ("schema", "checksum", "business_rules")
            valid: Whether validation passed
            details: Validation details (errors, warnings, statistics)
            correlation_id: Optional correlation ID
        """
        event = {
            "type": "data_validation",
            "timestamp": datetime.now().isoformat(),
            "module": module_name,
            "validation_type": validation_type,
            "valid": valid,
            "details": details,
            "correlation_id": correlation_id,
        }
        self._emit(event)

    def emit_symbol_evaluation_detail(
        self,
        symbol: str,
        stage: str,
        metrics: dict[str, Any],
        passed: bool,
        blocking_reason: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Emit detailed evaluation event for a symbol.

        Args:
            symbol: Symbol being evaluated
            stage: Evaluation stage (e.g., "bars_loaded", "gap_checked", "rvol_calculated")
            metrics: Metrics at this stage
            passed: Whether this stage passed
            blocking_reason: If not passed, why
            correlation_id: Optional correlation ID
        """
        event = {
            "type": "symbol_evaluation_detail",
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "stage": stage,
            "metrics": metrics,
            "passed": passed,
            "blocking_reason": blocking_reason,
            "correlation_id": correlation_id,
        }
        self._emit(event)

    def _emit(self, event: dict) -> None:
        """Emit event to all registered callbacks and event store."""
        logger.debug(f"Telemetry: {event['type']} - {event.get('module', event.get('symbol', 'n/a'))}")

        # Call all registered callbacks
        for callback in self.callbacks:
            try:
                callback(event)
            except Exception as e:
                logger.error(f"Telemetry callback failed: {e}")

        # Emit to event store if available
        if self.event_store:
            try:
                # Import here to avoid circular dependency
                from storage.event_schema import ModuleTickEvent, DataValidationEvent

                if event["type"] == "module_tick":
                    self.event_store.emit(
                        ModuleTickEvent(
                            timestamp=datetime.now(),
                            module=event["module"],
                            stage=event["stage"],
                            duration_ms=event["metrics"]["duration_ms"]
                            if event["metrics"]
                            else 0,
                            input_count=event["metrics"]["input_count"]
                            if event["metrics"]
                            else 0,
                            output_count=event["metrics"]["output_count"]
                            if event["metrics"]
                            else 0,
                            metrics=event["metrics"]["custom_metrics"]
                            if event["metrics"]
                            else {},
                            errors=event["errors"],
                            correlation_id=event.get("correlation_id"),
                        )
                    )
                elif event["type"] == "data_validation":
                    self.event_store.emit(
                        DataValidationEvent(
                            timestamp=datetime.now(),
                            module=event["module"],
                            validation_type=event["validation_type"],
                            valid=event["valid"],
                            details=event["details"],
                            correlation_id=event.get("correlation_id"),
                        )
                    )
            except Exception as e:
                logger.error(f"Failed to emit telemetry event to store: {e}")


# Global instance (can be overridden in tests)
_telemetry_emitter: TelemetryEmitter | None = None


def set_telemetry_emitter(emitter: TelemetryEmitter) -> None:
    """Set the global telemetry emitter."""
    global _telemetry_emitter
    _telemetry_emitter = emitter


def get_telemetry_emitter() -> TelemetryEmitter:
    """Get the global telemetry emitter."""
    global _telemetry_emitter
    if _telemetry_emitter is None:
        _telemetry_emitter = TelemetryEmitter()
    return _telemetry_emitter


def emit_module_tick(
    module_name: str,
    stage: str,
    metrics: ModuleMetrics | None = None,
    errors: list[dict] | None = None,
    correlation_id: str | None = None,
) -> None:
    """Convenience function to emit module tick."""
    get_telemetry_emitter().emit_module_tick(module_name, stage, metrics, errors, correlation_id)


def emit_data_validation(
    module_name: str,
    validation_type: str,
    valid: bool,
    details: dict,
    correlation_id: str | None = None,
) -> None:
    """Convenience function to emit data validation."""
    get_telemetry_emitter().emit_data_validation(
        module_name, validation_type, valid, details, correlation_id
    )


def emit_symbol_evaluation_detail(
    symbol: str,
    stage: str,
    metrics: dict[str, Any],
    passed: bool,
    blocking_reason: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """Convenience function to emit symbol evaluation detail."""
    get_telemetry_emitter().emit_symbol_evaluation_detail(
        symbol, stage, metrics, passed, blocking_reason, correlation_id
    )
