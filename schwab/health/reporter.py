"""Schwab broker health reporter — emits broker_health_changed events."""

from __future__ import annotations

import logging
from datetime import datetime

from schwab.auth.lifecycle import TokenLifecycle
from schwab.health.models import HealthCheck, HealthReport, HealthStatus
from schwab.settings import SchwabSettings
from storage.event_schema import BrokerHealthChangedEvent, EventMode
from storage.event_store import EventStore

logger = logging.getLogger(__name__)


class HealthReporter:
    def __init__(
        self,
        store: EventStore | None = None,
        settings: SchwabSettings | None = None,
        lifecycle: TokenLifecycle | None = None,
        session_id: str | None = None,
    ):
        self.store = store
        self.settings = settings or SchwabSettings.from_env()
        self.lifecycle = lifecycle or TokenLifecycle(self.settings)
        self.session_id = session_id
        self._last_status: HealthStatus | None = None

    def check(self) -> HealthReport:
        checks: list[HealthCheck] = []
        if self.settings.has_broker_credentials or self.settings.has_market_data_credentials:
            checks.append(HealthCheck("credentials", HealthStatus.HEALTHY))
        else:
            checks.append(
                HealthCheck(
                    "credentials",
                    HealthStatus.DOWN,
                    "no SCHWAB_* app keys configured",
                )
            )
        auth = self.lifecycle.status()
        if auth.get("authenticated") and not auth.get("expired"):
            checks.append(HealthCheck("token", HealthStatus.HEALTHY))
        elif auth.get("authenticated"):
            checks.append(
                HealthCheck("token", HealthStatus.DEGRADED, "token expired; refresh pending")
            )
        else:
            checks.append(
                HealthCheck(
                    "token", HealthStatus.UNAUTHENTICATED,
                    str(auth.get("reason") or "not authenticated"),
                )
            )

        statuses = {c.status for c in checks}
        if statuses == {HealthStatus.HEALTHY}:
            overall = HealthStatus.HEALTHY
        elif HealthStatus.DOWN in statuses:
            overall = HealthStatus.DOWN
        elif HealthStatus.UNAUTHENTICATED in statuses:
            overall = HealthStatus.UNAUTHENTICATED
        else:
            overall = HealthStatus.DEGRADED

        report = HealthReport(broker_name="schwab", status=overall, checks=checks)
        self._maybe_emit(report)
        return report

    def _maybe_emit(self, report: HealthReport) -> None:
        if self.store is None or report.status == self._last_status:
            self._last_status = report.status
            return
        try:
            self.store.emit(
                BrokerHealthChangedEvent(
                    timestamp=datetime.now(),
                    mode=EventMode.LIVE,
                    correlation_id=self.session_id,
                    message=f"Schwab health: {report.status.value}",
                    broker_name="schwab",
                    previous_health=self._last_status.value
                    if self._last_status
                    else "unknown",
                    new_health=report.status.value,
                    health_reason="; ".join(
                        f"{c.name}={c.status.value}" for c in report.checks
                    ),
                    payload=report.to_dict(),
                )
            )
        except Exception:
            logger.exception("failed to emit broker health event")
        self._last_status = report.status
