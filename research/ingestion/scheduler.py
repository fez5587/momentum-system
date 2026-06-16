"""Tiny interval scheduler used by the orchestrator and research jobs.

Each task has its own interval; ``run_pending`` executes whatever is due.
Task failures are recorded and never propagate, so one flaky data source
cannot stall the trading loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ScheduledTask:
    name: str
    func: Callable[[], object]
    interval_seconds: float
    enabled: bool = True
    last_run: float = 0.0
    last_error: str | None = None
    run_count: int = 0
    error_count: int = 0

    def due(self, now: float) -> bool:
        return self.enabled and (now - self.last_run) >= self.interval_seconds


@dataclass
class Scheduler:
    tasks: list[ScheduledTask] = field(default_factory=list)

    def add(
        self,
        name: str,
        func: Callable[[], object],
        interval_seconds: float,
        enabled: bool = True,
    ) -> ScheduledTask:
        task = ScheduledTask(name, func, interval_seconds, enabled=enabled)
        self.tasks.append(task)
        return task

    def run_pending(self, now: float | None = None) -> dict[str, object]:
        """Run all due tasks once. Returns {task_name: result_or_error}."""
        now = time.monotonic() if now is None else now
        results: dict[str, object] = {}
        for task in self.tasks:
            if not task.due(now):
                continue
            task.last_run = now
            task.run_count += 1
            try:
                results[task.name] = task.func()
                task.last_error = None
            except Exception as exc:  # noqa: BLE001
                task.error_count += 1
                task.last_error = str(exc)
                results[task.name] = exc
        return results

    def run_forever(self, tick_seconds: float = 1.0, stop_check=None) -> None:
        while True:
            if stop_check and stop_check():
                return
            self.run_pending()
            time.sleep(tick_seconds)
