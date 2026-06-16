"""Broker health models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNAUTHENTICATED = "unauthenticated"


@dataclass
class HealthCheck:
    name: str
    status: HealthStatus
    detail: str | None = None


@dataclass
class HealthReport:
    broker_name: str
    status: HealthStatus
    checks: list[HealthCheck] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "broker_name": self.broker_name,
            "status": self.status.value,
            "checks": [
                {"name": c.name, "status": c.status.value, "detail": c.detail}
                for c in self.checks
            ],
        }
