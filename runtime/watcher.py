"""Intraday watcher (Milestone 4).

Pulls candidates from a watchlist provider, evaluates each with the pure
strategy engine, drives a per-symbol state machine, and emits canonical
events into the event store:

    discovered -> watching -> ready | blocked   (re-evaluated each tick)

Signals are debounced: a symbol emits signal_ready at most once per session
unless it falls back to blocked first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Callable, Protocol

import pandas as pd

from storage.event_schema import (
    CriteriaEvaluatedEvent,
    EventMode,
    SignalBlockedEvent,
    SignalReadyEvent,
    SymbolDiscoveredEvent,
    SymbolStateChangedEvent,
)
from storage.event_store import EventStore
from strategy.evaluation.setup_evaluator import evaluate_setup

logger = logging.getLogger(__name__)


@dataclass
class WatchCandidate:
    """One symbol the watcher should evaluate."""

    symbol: str
    last_price: float | None = None
    previous_close: float | None = None
    avg_daily_volume: float | None = None
    source: str = "research"


class WatchlistProvider(Protocol):
    """Source of candidates and their session bars."""

    def get_candidates(self, session_date: date) -> list[WatchCandidate]: ...

    def get_bars(self, symbol: str, session_date: date) -> pd.DataFrame: ...


@dataclass
class WatcherConfig:
    session_id: str = ""
    mode: EventMode = EventMode.PAPER
    max_symbols: int = 25
    min_bars: int = 10
    ready_score_pct: float = 60.0
    min_quality: float = 0.30


@dataclass
class WatcherTickResult:
    session_date: str
    evaluated: int = 0
    ready: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    discovered: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Watcher:
    """Event-emitting intraday watcher."""

    def __init__(
        self,
        store: EventStore,
        provider: WatchlistProvider,
        config: WatcherConfig | None = None,
        catalyst_score_provider: Callable[[str], float | None] | None = None,
        spread_provider: Callable[[str], float | None] | None = None,
    ):
        self.store = store
        self.provider = provider
        self.config = config or WatcherConfig()
        # optional symbol -> 0..1 catalyst score (or None). Injected so the pure
        # evaluator stays DB-free; None provider == today's behavior exactly.
        self.catalyst_score_provider = catalyst_score_provider
        # optional symbol -> decision-time bid/ask spread fraction (or None).
        # Observe-only: it is logged onto the evaluation/ready events but does NOT
        # gate. None provider == today's behavior exactly (spread_pct logs as None,
        # matching the always-null minute_bars.spread_pct column).
        self.spread_provider = spread_provider
        if not self.config.session_id:
            self.config.session_id = f"session-{date.today().isoformat()}"
        # symbol -> current state for this session
        self._states: dict[str, str] = {}

    # -- state helpers -------------------------------------------------

    def _transition(self, symbol: str, new_state: str, reason: str | None) -> None:
        previous = self._states.get(symbol)
        if previous == new_state:
            return
        self._states[symbol] = new_state
        self.store.emit(
            SymbolStateChangedEvent(
                timestamp=datetime.now(),
                mode=self.config.mode,
                correlation_id=self.config.session_id,
                message=f"{symbol}: {previous or 'new'} -> {new_state}"
                + (f" ({reason})" if reason else ""),
                symbol=symbol,
                previous_state=previous,
                new_state=new_state,
                state_reason=reason,
                payload={"symbol": symbol, "previous_state": previous,
                         "new_state": new_state, "state_reason": reason},
            )
        )

    def _discover(self, candidate: WatchCandidate) -> None:
        if candidate.symbol in self._states:
            return
        self.store.emit(
            SymbolDiscoveredEvent(
                timestamp=datetime.now(),
                mode=self.config.mode,
                correlation_id=self.config.session_id,
                message=f"Discovered {candidate.symbol} via {candidate.source}",
                symbol=candidate.symbol,
                symbol_data={
                    "last_price": candidate.last_price,
                    "previous_close": candidate.previous_close,
                    "avg_daily_volume": candidate.avg_daily_volume,
                    "source": candidate.source,
                },
                payload={"symbol": candidate.symbol, "source": candidate.source},
            )
        )
        self._transition(candidate.symbol, "watching", "added to watchlist")

    # -- main loop body ------------------------------------------------

    def tick(self, session_date: date | None = None) -> WatcherTickResult:
        """One evaluation pass over all candidates."""
        session_date = session_date or date.today()
        result = WatcherTickResult(session_date=session_date.isoformat())

        try:
            candidates = self.provider.get_candidates(session_date)
        except Exception as exc:  # provider failure should not kill the loop
            logger.exception("watchlist provider failed")
            result.errors.append(f"provider: {exc}")
            return result

        for candidate in candidates[: self.config.max_symbols]:
            symbol = candidate.symbol
            try:
                if symbol not in self._states:
                    self._discover(candidate)
                    result.discovered.append(symbol)

                bars = self.provider.get_bars(symbol, session_date)
                if bars is None or len(bars) < self.config.min_bars:
                    self._transition(
                        symbol, "watching",
                        f"waiting for data ({0 if bars is None else len(bars)} bars)",
                    )
                    continue

                # Evaluate the setup as of the latest bar's timestamp, not the
                # wall clock. Minute bars are stored UTC-naive, but the entry
                # cutoff reasons in US/Eastern wall-clock, so convert. This
                # keeps replay and backfilled data honest (a 09:40 ET bar is
                # judged against the 09:40 cutoff regardless of run time).
                eval_time = None
                try:
                    from datetime import timezone as _tz
                    from zoneinfo import ZoneInfo as _ZI

                    last_ts = pd.Timestamp(bars["timestamp"].iloc[-1]).to_pydatetime()
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.replace(tzinfo=_tz.utc)
                    eval_time = last_ts.astimezone(_ZI("America/New_York")).replace(
                        tzinfo=None
                    )
                except Exception:  # noqa: BLE001 - fall back to now()
                    eval_time = None

                # optional LLM catalyst score (None unless the blend is enabled
                # AND a provider is injected) — never raises into the loop.
                cat_score = None
                if self.catalyst_score_provider is not None:
                    try:
                        cat_score = self.catalyst_score_provider(symbol)
                    except Exception:  # noqa: BLE001
                        cat_score = None

                evaluation = evaluate_setup(
                    bars,
                    previous_close=candidate.previous_close,
                    avg_daily_volume=candidate.avg_daily_volume,
                    ready_score_pct=self.config.ready_score_pct,
                    evaluation_time=eval_time,
                    min_bars=self.config.min_bars,
                    catalyst_score=cat_score,
                )
                result.evaluated += 1

                # Decision-time bid/ask spread (observe-only). Guarded like the
                # catalyst provider so a quote blip never kills the loop; None
                # when no provider is injected or the quote is missing/bad.
                spread_pct = None
                if self.spread_provider is not None:
                    try:
                        spread_pct = self.spread_provider(symbol)
                    except Exception:  # noqa: BLE001
                        spread_pct = None

                self.store.emit(
                    CriteriaEvaluatedEvent(
                        timestamp=datetime.now(),
                        mode=self.config.mode,
                        correlation_id=self.config.session_id,
                        message=(
                            f"{symbol} criteria {evaluation.criteria_passed}/"
                            f"{evaluation.criteria_total} "
                            f"({evaluation.success_score_pct:.0f}%)"
                        ),
                        symbol=symbol,
                        criteria_results={
                            "passed": evaluation.criteria_names_passed,
                            "failed": evaluation.criteria_names_failed,
                            "detail": evaluation.criteria_detail,
                            "gap_pct": evaluation.gap_pct,
                            "relative_volume": evaluation.relative_volume,
                            "spread_pct": spread_pct,
                            "status": evaluation.status,
                            "reason": evaluation.reason,
                        },
                        total_criteria=evaluation.criteria_total,
                        passed_criteria=evaluation.criteria_passed,
                        success_score_pct=evaluation.success_score_pct,
                        payload={
                            "symbol": symbol,
                            "success_score_pct": evaluation.success_score_pct,
                            "status": evaluation.status,
                            "reason": evaluation.reason,
                            "gap_pct": evaluation.gap_pct,
                            "relative_volume": evaluation.relative_volume,
                            "spread_pct": spread_pct,
                        },
                    )
                )

                if evaluation.status == "ready" and evaluation.setups:
                    result.ready.append(symbol)
                    if self._states.get(symbol) != "ready":
                        setup = evaluation.setups[0]
                        self._transition(symbol, "ready", setup["setup_type"])
                        signal_data = {**setup, "spread_pct": spread_pct}
                        self.store.emit(
                            SignalReadyEvent(
                                timestamp=datetime.now(),
                                mode=self.config.mode,
                                correlation_id=self.config.session_id,
                                message=(
                                    f"{symbol} READY {setup['setup_type']} "
                                    f"entry {setup['entry_price']} stop "
                                    f"{setup['stop_loss_price']}"
                                ),
                                symbol=symbol,
                                signal_type=setup["setup_type"],
                                confidence=setup["confidence"],
                                signal_data=signal_data,
                                payload={
                                    "symbol": symbol,
                                    "signal_type": setup["setup_type"],
                                    "confidence": setup["confidence"],
                                    "signal_data": signal_data,
                                },
                            )
                        )
                else:
                    status = "blocked" if evaluation.status != "late" else "late"
                    result.blocked.append(symbol)
                    if self._states.get(symbol) == "ready":
                        # fell out of ready — allow re-signal later
                        self._transition(symbol, status, evaluation.reason)
                    elif self._states.get(symbol) != status:
                        self._transition(symbol, status, evaluation.reason)
                    self.store.emit(
                        SignalBlockedEvent(
                            timestamp=datetime.now(),
                            mode=self.config.mode,
                            correlation_id=self.config.session_id,
                            message=f"{symbol} blocked: {evaluation.reason}",
                            symbol=symbol,
                            blocking_reason=evaluation.reason or "unknown",
                            unmet_criteria=evaluation.criteria_names_failed,
                            payload={
                                "symbol": symbol,
                                "blocking_reason": evaluation.reason,
                                "unmet_criteria": evaluation.criteria_names_failed,
                            },
                        )
                    )
            except Exception as exc:
                logger.exception("watcher tick failed for %s", symbol)
                result.errors.append(f"{symbol}: {exc}")

        return result
