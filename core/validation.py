"""Data validation utilities for module boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from core.contracts import ValidationFailure


@dataclass
class SchemaValidator:
    """Validate data against a schema."""

    required_fields: dict[str, type]  # {field_name: expected_type}
    optional_fields: dict[str, type] | None = None

    def __post_init__(self):
        if self.optional_fields is None:
            self.optional_fields = {}

    def validate(self, data: dict) -> tuple[bool, list[dict]]:
        """Validate data against schema.

        Args:
            data: Dictionary to validate

        Returns:
            (is_valid, list of failure dicts)
        """
        failures = []

        # Check required fields
        for field_name, expected_type in self.required_fields.items():
            if field_name not in data:
                failures.append(
                    {
                        "field": field_name,
                        "value": None,
                        "reason": "Required field missing",
                        "severity": "error",
                    }
                )
            elif not isinstance(data[field_name], expected_type):
                failures.append(
                    {
                        "field": field_name,
                        "value": data[field_name],
                        "reason": f"Expected {expected_type.__name__}, got {type(data[field_name]).__name__}",
                        "severity": "error",
                    }
                )

        # Check optional fields if present
        if self.optional_fields:
            for field_name, expected_type in self.optional_fields.items():
                if field_name in data and not isinstance(data[field_name], expected_type):
                    failures.append(
                        {
                            "field": field_name,
                            "value": data[field_name],
                            "reason": f"Expected {expected_type.__name__}, got {type(data[field_name]).__name__}",
                            "severity": "warning",
                        }
                    )

        return len([f for f in failures if f.severity == "error"]) == 0, failures


class RangeValidator:
    """Validate numeric values are within expected ranges."""

    def __init__(self, min_val: float | None = None, max_val: float | None = None):
        self.min_val = min_val
        self.max_val = max_val

    def validate(self, value: float, field_name: str = "value") -> tuple[bool, dict | None]:
        """Validate value is in range.

        Args:
            value: Numeric value to validate
            field_name: Name of field for error message

        Returns:
            (is_valid, failure dict or None)
        """
        if self.min_val is not None and value < self.min_val:
            return False, {
                "field": field_name,
                "value": value,
                "reason": f"Value {value} below minimum {self.min_val}",
                "severity": "error",
            }

        if self.max_val is not None and value > self.max_val:
            return False, {
                "field": field_name,
                "value": value,
                "reason": f"Value {value} above maximum {self.max_val}",
                "severity": "error",
            }

        return True, None


class ChecksumValidator:
    """Validate data integrity using checksums."""

    @staticmethod
    def compute_checksum(data: dict | list) -> str:
        """Compute simple checksum for data.

        Args:
            data: Dictionary or list to checksum

        Returns:
            Hex checksum string
        """
        import hashlib
        import json

        data_str = json.dumps(data, sort_keys=True, default=str)
        return hashlib.md5(data_str.encode()).hexdigest()

    @staticmethod
    def validate_checksum(
        data: dict | list, expected_checksum: str
    ) -> tuple[bool, dict | None]:
        """Validate data matches checksum.

        Args:
            data: Data to validate
            expected_checksum: Expected checksum value

        Returns:
            (is_valid, failure dict or None)
        """
        actual_checksum = ChecksumValidator.compute_checksum(data)
        if actual_checksum != expected_checksum:
            return False, {
                "field": "checksum",
                "value": actual_checksum,
                "reason": f"Checksum mismatch: expected {expected_checksum}, got {actual_checksum}",
                "severity": "error",
            }
        return True, None


class BusinessRuleValidator:
    """Validate data against business logic rules."""

    def __init__(self):
        self.rules: dict[str, Callable[[Any], tuple[bool, str]]] = {}

    def add_rule(self, rule_name: str, rule_fn: Callable[[Any], tuple[bool, str]]) -> None:
        """Add a business rule.

        Args:
            rule_name: Name of the rule
            rule_fn: Function that takes data and returns (is_valid, reason)
        """
        self.rules[rule_name] = rule_fn

    def validate(self, data: Any) -> tuple[bool, list[dict]]:
        """Apply all business rules.

        Args:
            data: Data to validate

        Returns:
            (is_valid, list of failure dicts)
        """
        failures = []
        for rule_name, rule_fn in self.rules.items():
            try:
                is_valid, reason = rule_fn(data)
                if not is_valid:
                    failures.append(
                        {
                            "field": "business_rule",
                            "value": None,
                            "reason": f"{rule_name}: {reason}",
                            "severity": "error",
                        }
                    )
            except Exception as e:
                failures.append(
                    {
                        "field": "business_rule",
                        "value": None,
                        "reason": f"{rule_name} raised exception: {e}",
                        "severity": "error",
                    }
                )

        return len(failures) == 0, failures


# Concrete validators for common cases

class BarDataValidator:
    """Validate OHLCV bar data."""

    @staticmethod
    def validate_bar(bar: dict) -> tuple[bool, list[dict]]:
        """Validate a single bar.

        Args:
            bar: Bar dictionary with {timestamp, open, high, low, close, volume}

        Returns:
            (is_valid, list of failure dicts)
        """
        failures = []

        # Check required fields
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        for field in required:
            if field not in bar:
                failures.append(
                    {
                        "field": field,
                        "value": None,
                        "reason": "Missing required field",
                        "severity": "error",
                    }
                )

        if failures:
            return False, failures

        # Check value consistency
        open_val = bar["open"]
        high_val = bar["high"]
        low_val = bar["low"]
        close_val = bar["close"]
        volume_val = bar["volume"]

        # High must be >= all others
        if high_val < open_val or high_val < close_val or high_val < low_val:
            failures.append(
                {
                    "field": "high",
                    "value": high_val,
                    "reason": f"High ({high_val}) must be >= open ({open_val}), close ({close_val}), low ({low_val})",
                    "severity": "error",
                }
            )

        # Low must be <= all others
        if low_val > open_val or low_val > close_val or low_val > high_val:
            failures.append(
                {
                    "field": "low",
                    "value": low_val,
                    "reason": f"Low ({low_val}) must be <= open ({open_val}), close ({close_val}), high ({high_val})",
                    "severity": "error",
                }
            )

        # Volume should be non-negative
        if volume_val < 0:
            failures.append(
                {
                    "field": "volume",
                    "value": volume_val,
                    "reason": "Volume cannot be negative",
                    "severity": "error",
                }
            )

        # Warn on zero volume
        if volume_val == 0:
            failures.append(
                {
                    "field": "volume",
                    "value": volume_val,
                    "reason": "Zero volume bar",
                    "severity": "warning",
                }
            )

        return len([f for f in failures if f.severity == "error"]) == 0, failures
