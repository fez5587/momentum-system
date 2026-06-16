"""Lightweight in-memory tracking of evaluation history per symbol."""

from __future__ import annotations

from collections import defaultdict


class EvaluationTracker:
    """Tracks recent evaluation results per symbol for debounce/analysis."""

    def __init__(self, max_history: int = 50):
        self.max_history = max_history
        self._history: dict[str, list[dict]] = defaultdict(list)

    def record(self, symbol: str, result: dict) -> None:
        history = self._history[symbol]
        history.append(result)
        if len(history) > self.max_history:
            del history[: len(history) - self.max_history]

    def last(self, symbol: str) -> dict | None:
        history = self._history.get(symbol)
        return history[-1] if history else None

    def consecutive_status(self, symbol: str, status: str) -> int:
        count = 0
        for result in reversed(self._history.get(symbol, [])):
            if result.get("status") == status:
                count += 1
            else:
                break
        return count
