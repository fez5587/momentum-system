"""Trading execution service (Milestone 4/5 glue).

Connects ready signals to broker execution with risk controls and an
approval workflow:

    signal_ready
      -> risk checks (sizing, max concurrent, daily-loss circuit breaker)
      -> order_approval_requested
      -> [manual approval in the dashboard]  or  [auto-approve]
      -> order_approved -> broker submit -> order_submitted (+ filled)

Exit orders close existing broker positions on demand.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime

from alpaca_paper.execution import (
    AlpacaPaperExecutor,
    ExecutionRequest,
)
from storage.event_schema import (
    EventMode,
    OrderApprovalRequestedEvent,
    OrderApprovedEvent,
    OrderRejectedEvent,
    RiskRuleTriggeredEvent,
)
from storage.event_store import EventStore
from storage.projections import (
    query_account_positions_snapshot,
    query_approval_queue,
    query_ready_signals_snapshot,
)
from strategy.risk.position_sizing import (
    PositionSizingConfig,
    calculate_position_size,
)
from trading_mode import TradingModeSettings

logger = logging.getLogger(__name__)


def _locked(method):
    """Serialize a mutating service method behind self._lock (a re-entrant lock).

    The fast trigger thread (submit_breakout_now) and the main loop (tick,
    approvals) both mutate execution state (_armed, _requested_symbols) and emit
    events; without this they would race on the shared psycopg2 connection and
    the in-memory sets. RLock so a public method may call another (tick ->
    expire_stale_entries) without deadlocking.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


@dataclass
class ExecutionSettings:
    enabled: bool = True
    auto_approve: bool = False
    max_orders_per_tick: int = 1
    max_concurrent_positions: int = 3
    risk_per_trade_pct: float = 0.01
    default_equity: float = 100_000.0
    max_daily_loss_pct: float = 0.03
    # on a daily-loss breach, also flatten open positions + cancel unfilled entries
    flatten_on_breach: bool = True
    # --- entry mechanism (Ross-Cameron-style; all tunable) -----------------
    # reward target as a multiple of risk (entry + reward_multiple * (entry-stop))
    reward_multiple: float = 2.0
    # how the entry order is placed: "limit" rests at the entry trigger so an
    # unfilled order is a real, cancellable state; "market" fills immediately
    entry_order_type: str = "limit"
    # cancel an unfilled entry after this many minutes of resting (the "back
    # out" time box; ~1 bar == 1 minute). 0 disables the timeout. Wall-clock
    # based, so the invalidation guard can run far more often than this.
    entry_timeout_bars: int = 2
    # cancel an unfilled entry if price trades back below the entry trigger by
    # this fraction. NOT 0.0: at zero tolerance any sub-cent wobble below the
    # trigger cancels the entry one tick after it fires, so a breakout that
    # oscillates around the level churns and never holds (observed live). Give
    # it room — the bracket STOP (opening-range low) is the real protection and
    # risk is capped at 1%/trade by sizing. Negative disables price-break cancel.
    entry_invalidate_pct: float = 0.015
    # live-trigger fast path (submit_breakout_now): how far above the trigger to
    # cap the marketable limit so a breakout FILLS on a runner instead of
    # resting forever at the trigger, while still bounding slippage.
    trigger_slippage_pct: float = 0.004

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ExecutionSettings":
        values = dict(os.environ)
        if env is not None:
            values.update(env)

        def flag(key: str, default: str) -> bool:
            return values.get(key, default).strip().lower() in {"1", "true", "yes", "on"}

        order_type = values.get("TRADING_ENTRY_ORDER_TYPE", "limit").strip().lower()
        if order_type not in {"limit", "market"}:
            order_type = "limit"

        return cls(
            enabled=flag("TRADING_EXECUTION_ENABLED", "1"),
            auto_approve=flag("TRADING_AUTO_APPROVE", "0"),
            max_orders_per_tick=int(values.get("TRADING_MAX_ORDERS_PER_TICK", "1")),
            max_concurrent_positions=int(
                values.get("TRADING_MAX_CONCURRENT_POSITIONS", "3")
            ),
            risk_per_trade_pct=float(values.get("TRADING_RISK_PER_TRADE_PCT", "0.01")),
            default_equity=float(values.get("TRADING_DEFAULT_EQUITY", "100000")),
            max_daily_loss_pct=float(values.get("TRADING_MAX_DAILY_LOSS_PCT", "0.03")),
            flatten_on_breach=flag("TRADING_FLATTEN_ON_BREACH", "1"),
            reward_multiple=float(values.get("TRADING_REWARD_MULTIPLE", "2.0")),
            entry_order_type=order_type,
            entry_timeout_bars=int(values.get("TRADING_ENTRY_TIMEOUT_BARS", "2")),
            entry_invalidate_pct=float(
                values.get("TRADING_ENTRY_INVALIDATE_PCT", "0.015")
            ),
            trigger_slippage_pct=float(
                values.get("TRADING_TRIGGER_SLIP_PCT", "0.004")
            ),
        )


class TradingExecutionService:
    """Drives the signal -> approval -> order pipeline each tick."""

    def __init__(
        self,
        store: EventStore,
        executor: AlpacaPaperExecutor | None = None,
        settings: ExecutionSettings | None = None,
        trading_mode: TradingModeSettings | None = None,
        session_id: str | None = None,
        equity: float | None = None,
        price_provider=None,
        now_fn=None,
    ):
        self.store = store
        self.settings = settings or ExecutionSettings.from_env()
        self.trading_mode = trading_mode or TradingModeSettings.from_env()
        self.session_id = session_id
        self.equity = equity or self.settings.default_equity
        self.executor = executor or AlpacaPaperExecutor(
            store, session_id=session_id
        )
        self.mode = EventMode.PAPER
        # optional callable: symbol -> latest price, used to invalidate unfilled
        # entries that break back below the entry trigger before filling
        self.price_provider = price_provider
        # injectable clock so the entry timeout is wall-clock based (and so the
        # invalidation check can run far more often than the timeout window
        # without changing the timeout's meaning); tests pass a fake clock
        self._now = now_fn or datetime.now
        # signals we've already requested approval for this session
        self._requested_symbols: set[str] = set()
        # armed (submitted, awaiting fill) entries we may need to cancel.
        # order_id -> {symbol, entry_price, broker_order_id, armed_at, checks}
        self._armed: dict[str, dict] = {}
        # daily-loss circuit breaker: once tripped, no new entries this session
        # (a REAL loss is permanent for the session — a bounce can't re-open it)
        self._halted = False
        # transient data-halt: equity unreadable (network/DNS). Blocks new entries
        # WHILE unreadable but RECOVERS on the next good read — a DNS blip must not
        # end the trading day the way a real loss does.
        self._data_halt = False
        # session closed (end-of-day flatten): no new entries for the rest of the day
        self._session_closed = False
        # consecutive equity-read failures; after the limit we pause (recoverable)
        self._equity_read_failures = 0
        self._equity_fail_limit = int(
            os.environ.get("TRADING_EQUITY_FAIL_LIMIT", "5")
        )
        # serializes state mutations across the main loop and the trigger thread
        self._lock = threading.RLock()

    # -- pipeline -----------------------------------------------------------

    def _open_position_count(self) -> int:
        snapshots = query_account_positions_snapshot(
            self.store, broker_name=self.executor.broker_name
        )
        if not snapshots:
            return 0
        return len(snapshots[-1].get("positions") or [])

    def _held_symbols(self) -> set[str]:
        snapshots = query_account_positions_snapshot(
            self.store, broker_name=self.executor.broker_name
        )
        if not snapshots:
            return set()
        return {
            str(p.get("symbol"))
            for p in snapshots[-1].get("positions") or []
            if p.get("symbol")
        }

    def _broker_held_symbols(self) -> set[str] | None:
        """Symbols the broker reports as OPEN POSITIONS right now (truth).

        Used to guard entry cancellation: our own order_filled events lag the
        actual fill (they arrive via the 60s account sync), so the entry-timeout
        guard once cancelled an entry it thought was unfilled AFTER it had really
        filled — and cancelling a bracket parent cancels its stop/take-profit
        legs, leaving the position NAKED. This queries the broker directly so a
        filled name is never cancelled. None if positions can't be read (caller
        then falls back to the synced snapshot and stays conservative).
        """
        client = getattr(self.executor, "client", None)
        if client is None or not hasattr(client, "get_positions"):
            return None
        try:
            positions = client.get_positions() or []
            return {str(p.get("symbol")) for p in positions if p.get("symbol")}
        except Exception as exc:  # noqa: BLE001
            logger.warning("broker positions unreadable in entry guard: %s", exc)
            return None

    def _flatten_all(self, reason: str) -> dict:
        """Cancel unfilled entries and market-close every open position.

        Used when the daily-loss circuit breaker trips: stop the bleeding by
        cancelling resting entries and flattening the book. Best-effort — errors
        are collected, not raised, so one bad symbol can't block the rest.
        ``close_position`` also cancels the position's resting bracket legs.
        """
        result: dict = {"cancelled_entries": 0, "closed_positions": [], "errors": []}
        # 1) cancel unfilled (armed) entries we are tracking
        for order_id in list(self._armed):
            armed = self._armed.pop(order_id)
            symbol = armed.get("symbol", "")
            try:
                self.executor.cancel_entry(
                    order_id, armed.get("broker_order_id"), symbol, reason
                )
                result["cancelled_entries"] += 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"cancel {symbol}: {exc}")
            self._requested_symbols.discard(symbol)
        # 2) market-close open positions
        client = getattr(self.executor, "client", None)
        for symbol in sorted(self._held_symbols()):
            if client is None or not hasattr(client, "close_position"):
                result["errors"].append(f"close {symbol}: no broker client to flatten")
                continue
            try:
                client.close_position(symbol)
                result["closed_positions"].append(symbol)  # only on a confirmed close
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"close {symbol}: {exc}")
        return result

    @_locked
    def _daily_loss_breach(self) -> bool:
        """True while new entries should be blocked.

        Two distinct halts: a REAL daily-loss breach (``_halted``) is permanent
        for the session so a bounce can't re-open the floodgates; an equity-read
        outage (``_data_halt``) blocks WHILE unreadable but recovers on the next
        good read — a transient DNS/network blip must not end the trading day.
        """
        if self._halted or self._session_closed:
            return True
        max_loss = abs(float(self.settings.max_daily_loss_pct or 0.0))
        if max_loss <= 0:
            return False
        client = getattr(self.executor, "client", None)
        if client is None:
            return False
        try:
            acct = client.get_account()
            equity_raw = acct.get("equity")
            if equity_raw is None:
                raise ValueError("account 'equity' field missing")
            equity = float(equity_raw)
            baseline = float(acct.get("last_equity") or equity)
            # good read -> clear the transient data-halt and resume
            if self._data_halt or self._equity_read_failures:
                if self._data_halt:
                    self.store.emit(
                        RiskRuleTriggeredEvent(
                            timestamp=datetime.now(), mode=self.mode,
                            correlation_id=self.session_id,
                            message="Equity readable again — resuming new entries",
                            rule_type="equity_recovered", rule_value=0.0,
                            current_state={"equity": equity},
                            action_taken="resumed_new_entries",
                        )
                    )
                self._data_halt = False
                self._equity_read_failures = 0
        except Exception as exc:  # noqa: BLE001
            self._equity_read_failures += 1
            logger.warning(
                "equity read failed (%d consecutive): %s",
                self._equity_read_failures, exc,
            )
            # PAUSE (recoverable): block new entries while equity is unreadable,
            # rather than silently disabling the most important safety control —
            # but DO NOT permanently halt the session for a transient outage.
            if self._equity_read_failures >= self._equity_fail_limit and not self._data_halt:
                self._data_halt = True
                self.store.emit(
                    RiskRuleTriggeredEvent(
                        timestamp=datetime.now(),
                        mode=self.mode,
                        correlation_id=self.session_id,
                        message=(
                            f"Equity unreadable {self._equity_read_failures}x — "
                            "pausing new entries until it recovers"
                        ),
                        rule_type="equity_unreadable",
                        rule_value=float(self._equity_read_failures),
                        current_state={"failures": self._equity_read_failures},
                        action_taken="paused_new_entries",
                    )
                )
            return self._data_halt
        if baseline <= 0:
            return False
        pnl_pct = (equity - baseline) / baseline
        if pnl_pct <= -max_loss:
            self._halted = True
            flat = (
                self._flatten_all("daily_loss_circuit_breaker")
                if self.settings.flatten_on_breach
                else {"closed_positions": [], "cancelled_entries": 0}
            )
            extra = ""
            action = "halted_new_entries"
            if self.settings.flatten_on_breach:
                n_closed = len(flat["closed_positions"])
                n_cancel = flat["cancelled_entries"]
                flat_errs = flat.get("errors") or []
                extra = (
                    f" — flattened {n_closed} position(s), "
                    f"cancelled {n_cancel} unfilled entr{'y' if n_cancel == 1 else 'ies'}"
                )
                if flat_errs:
                    extra += f"; FLATTEN INCOMPLETE: {flat_errs}"
                action = "halted_flatten_incomplete" if flat_errs else "halted_and_flattened"
            self.store.emit(
                RiskRuleTriggeredEvent(
                    timestamp=datetime.now(),
                    mode=self.mode,
                    correlation_id=self.session_id,
                    message=(
                        f"Daily-loss circuit breaker tripped: {pnl_pct:+.2%} "
                        f"(limit -{max_loss:.0%}) — halting new entries{extra}"
                    ),
                    rule_type="daily_loss",
                    rule_value=max_loss,
                    current_state={
                        "equity": equity,
                        "baseline_equity": baseline,
                        "pnl_pct": round(pnl_pct, 4),
                        "flatten": flat,
                    },
                    action_taken=action,
                )
            )
        return self._halted

    @_locked
    def request_approvals_for_ready_signals(self) -> list[str]:
        """Turn fresh ready signals into approval requests. Returns order ids."""
        if not self.settings.enabled:
            return []

        if self._daily_loss_breach():
            return []

        if self._open_position_count() >= self.settings.max_concurrent_positions:
            self.store.emit(
                RiskRuleTriggeredEvent(
                    timestamp=datetime.now(),
                    mode=self.mode,
                    correlation_id=self.session_id,
                    message="Max concurrent positions reached — no new entries",
                    rule_type="max_concurrent_positions",
                    rule_value=float(self.settings.max_concurrent_positions),
                    current_state={"open_positions": self._open_position_count()},
                    action_taken="skipped_new_entries",
                )
            )
            return []

        held = self._held_symbols()
        pending = {row["symbol"] for row in query_approval_queue(self.store)}
        signals = query_ready_signals_snapshot(self.store, session_id=self.session_id)

        created: list[str] = []
        for signal in signals:
            if len(created) >= self.settings.max_orders_per_tick:
                break
            symbol = signal["symbol"]
            if symbol in held or symbol in pending or symbol in self._requested_symbols:
                continue
            entry = signal.get("entry_price")
            stop = signal.get("stop_loss_price")
            if not entry or not stop or stop >= entry:
                continue

            sizing = calculate_position_size(
                float(entry),
                float(stop),
                equity=self.equity,
                config=PositionSizingConfig(
                    risk_per_trade_pct=self.settings.risk_per_trade_pct,
                    default_equity=self.settings.default_equity,
                ),
            )
            if sizing.position_size <= 0:
                continue

            entry_f = float(entry)
            stop_f = float(stop)
            risk_per_share = entry_f - stop_f
            request = ExecutionRequest(
                symbol=symbol,
                side="buy",
                quantity=sizing.position_size,
                entry_price=entry_f,
                stop_loss_price=stop_f,
                take_profit_price=round(
                    entry_f + self.settings.reward_multiple * risk_per_share, 2
                ),
                order_type=self.settings.entry_order_type,
            )
            approval_mode = "auto" if self.settings.auto_approve else "manual"
            self.store.emit(
                OrderApprovalRequestedEvent(
                    timestamp=datetime.now(),
                    mode=self.mode,
                    correlation_id=self.session_id,
                    message=(
                        f"Approval requested ({approval_mode}): buy "
                        f"{request.quantity} {symbol} @ ~{entry} stop {stop}"
                    ),
                    order_id=request.order_id,
                    symbol=symbol,
                    requested_by="trading_execution",
                    approval_mode=approval_mode,
                    execution_mode=self.trading_mode.execution_mode,
                    execution_request=request.to_payload(),
                )
            )
            self._requested_symbols.add(symbol)
            created.append(request.order_id)
            logger.info("approval requested for %s (%s)", symbol, request.order_id)

        return created

    @_locked
    def approve_order(
        self, order_id: str, approved_by: str = "dashboard", notes: str | None = None
    ) -> dict:
        """Approve a pending order and execute it."""
        entry = self._find_pending(order_id)
        if entry is None:
            return {"ok": False, "error": f"order {order_id} not pending"}
        request = ExecutionRequest.from_payload(entry["execution_request"])
        self.store.emit(
            OrderApprovedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=f"Order {order_id} approved by {approved_by}",
                order_id=order_id,
                symbol=request.symbol,
                approved_by=approved_by,
                approval_notes=notes,
            )
        )
        result = self.executor.execute(request)
        # If the entry didn't fill immediately (a resting limit at the trigger),
        # arm it so expire_stale_entries() can back out on timeout or a break
        # below the entry. Market orders that fill on submit are never armed.
        # Only arm genuinely OPEN resting entries — never a filled, partial, or
        # rejected order (a rejected entry would otherwise occupy a position slot
        # forever and a partial would be backed out while real shares are held).
        if (
            result.ok
            and result.status in {"new", "accepted", "pending_new", "held", "accepted_for_bidding"}
            and request.entry_price
        ):
            self._armed[order_id] = {
                "symbol": request.symbol,
                "entry_price": float(request.entry_price),
                "broker_order_id": result.broker_order_id,
                "armed_at": self._now(),
                "checks": 0,
            }
        return {
            "ok": result.ok,
            "order_id": order_id,
            "broker_order_id": result.broker_order_id,
            "status": result.status,
            "error": result.error,
        }

    @_locked
    def submit_breakout_now(
        self,
        symbol: str,
        trigger: float,
        stop: float,
        last_price: float | None = None,
        reason: str = "orb_live_break",
    ) -> dict:
        """Fire a breakout entry immediately on a LIVE price cross.

        The disciplined-but-fast counterpart to the watcher->approval cadence:
        used by the armed-trigger loop when price crosses a pre-computed
        opening-range high. Runs the SAME risk gates as the slow path (daily-loss
        breaker, max-concurrent, per-symbol dedup, 1%-risk sizing), but submits a
        *marketable* limit (capped a hair above the trigger) so it fills on a
        runner instead of resting at the level. Returns a result dict; ``skipped``
        explains a no-op (the symbol can simply try again next tick).
        """
        if not self.settings.enabled:
            return {"ok": False, "skipped": "disabled"}
        if self._daily_loss_breach():
            return {"ok": False, "skipped": "halted"}
        if self._open_position_count() >= self.settings.max_concurrent_positions:
            return {"ok": False, "skipped": "max_positions"}

        # per-symbol dedup shared with the slow path: never double-enter a name
        # that's already held, pending approval, or requested this session.
        held = self._held_symbols()
        pending = {row["symbol"] for row in query_approval_queue(self.store)}
        if symbol in held or symbol in pending or symbol in self._requested_symbols:
            return {"ok": False, "skipped": "already_active"}

        entry_ref = float(last_price or trigger)
        stop_f = float(stop)
        trigger_f = float(trigger)
        if stop_f >= entry_ref or trigger_f <= 0:
            return {"ok": False, "skipped": "bad_geometry"}

        sizing = calculate_position_size(
            entry_ref,
            stop_f,
            equity=self.equity,
            config=PositionSizingConfig(
                risk_per_trade_pct=self.settings.risk_per_trade_pct,
                default_equity=self.settings.default_equity,
            ),
        )
        if sizing.position_size <= 0:
            return {"ok": False, "skipped": "zero_size"}

        # marketable limit: fill now (price is already at/above the trigger), but
        # cap how far above we'll chase so a gap-through doesn't pay any price.
        limit_price = round(
            max(entry_ref, trigger_f) * (1.0 + self.settings.trigger_slippage_pct), 2
        )
        risk_per_share = entry_ref - stop_f
        request = ExecutionRequest(
            symbol=symbol,
            side="buy",
            quantity=sizing.position_size,
            entry_price=limit_price,
            stop_loss_price=stop_f,
            take_profit_price=round(
                entry_ref + self.settings.reward_multiple * risk_per_share, 2
            ),
            order_type="limit",
        )
        # audit trail mirrors the slow path: requested(auto) -> approved -> submit
        self.store.emit(
            OrderApprovalRequestedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=(
                    f"Live ORB break ({reason}): buy {request.quantity} {symbol} "
                    f"@ ~{entry_ref:.2f} (trigger {trigger_f:.2f}, stop {stop_f:.2f})"
                ),
                order_id=request.order_id,
                symbol=symbol,
                requested_by="orb_trigger",
                approval_mode="auto",
                execution_mode=self.trading_mode.execution_mode,
                execution_request=request.to_payload(),
            )
        )
        self.store.emit(
            OrderApprovedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=f"Order {request.order_id} auto-approved (live ORB break)",
                order_id=request.order_id,
                symbol=symbol,
                approved_by="orb_trigger",
                approval_notes=reason,
            )
        )
        self._requested_symbols.add(symbol)
        result = self.executor.execute(request)
        if (
            result.ok
            and result.status
            in {"new", "accepted", "pending_new", "held", "accepted_for_bidding"}
            and request.entry_price
        ):
            self._armed[request.order_id] = {
                "symbol": symbol,
                "entry_price": float(request.entry_price),
                "broker_order_id": result.broker_order_id,
                "armed_at": self._now(),
                "checks": 0,
            }
        logger.info(
            "live ORB break %s qty=%s -> ok=%s status=%s",
            symbol, request.quantity, result.ok, result.status,
        )
        return {
            "ok": result.ok,
            "order_id": request.order_id,
            "broker_order_id": result.broker_order_id,
            "status": result.status,
            "error": result.error,
            "quantity": sizing.position_size,
            "entry": limit_price,
            "stop": stop_f,
        }

    @_locked
    def reject_order(
        self, order_id: str, rejected_by: str = "dashboard", reason: str = "manual"
    ) -> dict:
        entry = self._find_pending(order_id)
        if entry is None:
            return {"ok": False, "error": f"order {order_id} not pending"}
        self.store.emit(
            OrderRejectedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=f"Order {order_id} rejected by {rejected_by}: {reason}",
                order_id=order_id,
                symbol=entry.get("symbol") or "",
                rejected_by=rejected_by,
                rejection_reason=reason,
            )
        )
        # allow the symbol to re-signal later
        self._requested_symbols.discard(entry.get("symbol") or "")
        return {"ok": True, "order_id": order_id}

    @_locked
    def process_auto_approvals(self) -> list[dict]:
        """Auto-approve any pending requests marked approval_mode=auto."""
        results = []
        for entry in query_approval_queue(self.store):
            if entry.get("approval_mode") == "auto":
                results.append(
                    self.approve_order(entry["order_id"], approved_by="auto")
                )
        return results

    @_locked
    def submit_exit_order(self, symbol: str, reason: str = "manual_exit") -> dict:
        """Close an open broker position for symbol with a market sell."""
        held = self._held_symbols()
        quantity = None
        snapshots = query_account_positions_snapshot(
            self.store, broker_name=self.executor.broker_name
        )
        if snapshots:
            for p in snapshots[-1].get("positions") or []:
                if str(p.get("symbol")) == symbol:
                    quantity = int(abs(float(p.get("quantity") or 0)))
        if symbol not in held or not quantity:
            return {"ok": False, "error": f"no open position for {symbol}"}
        # Prefer close_position: closes the FULL position regardless of long/short
        # sign and fractional qty. A plain 'sell' of abs(qty) would DOUBLE a short
        # position, and int-truncation would leave residual shares un-flat.
        client = getattr(self.executor, "client", None)
        if client is not None and hasattr(client, "close_position"):
            try:
                raw = client.close_position(symbol) or {}
                return {
                    "ok": True,
                    "order_id": str(raw.get("id") or f"close-{symbol}"),
                    "broker_order_id": raw.get("id"),
                    "status": raw.get("status") or "closing",
                    "error": None,
                    "reason": reason,
                }
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc), "reason": reason}
        request = ExecutionRequest(symbol=symbol, side="sell", quantity=quantity)
        result = self.executor.execute(request)
        return {
            "ok": result.ok,
            "order_id": request.order_id,
            "broker_order_id": result.broker_order_id,
            "status": result.status,
            "error": result.error,
            "reason": reason,
        }

    @_locked
    def close_session(self, reason: str = "eod_flatten") -> dict:
        """End-of-day: block new entries and flatten the book.

        A day-trading strategy shouldn't carry overnight gap risk, so at the
        configured time the loop calls this to cancel resting entries, market-
        close every open position (which also cancels its bracket legs), and
        halt new entries for the rest of the session.
        """
        self._session_closed = True
        result = self._flatten_all(reason)
        # belt-and-suspenders: close any broker position the store snapshot missed
        client = getattr(self.executor, "client", None)
        if client is not None and hasattr(client, "get_positions"):
            try:
                for p in client.get_positions():
                    sym = p.get("symbol")
                    if sym and sym not in result["closed_positions"]:
                        try:
                            client.close_position(sym)
                            result["closed_positions"].append(sym)
                        except Exception as exc:  # noqa: BLE001
                            result["errors"].append(f"close {sym}: {exc}")
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"positions: {exc}")
        self.store.emit(
            RiskRuleTriggeredEvent(
                timestamp=datetime.now(), mode=self.mode, correlation_id=self.session_id,
                message=(f"End-of-day flatten ({reason}): closed "
                         f"{len(result['closed_positions'])} position(s), "
                         f"cancelled {result['cancelled_entries']} entr"
                         f"{'y' if result['cancelled_entries'] == 1 else 'ies'}"
                         + (f"; ERRORS {result['errors']}" if result['errors'] else "")),
                rule_type="eod_flatten", rule_value=0.0,
                current_state=result, action_taken="closed_session",
            )
        )
        return result

    def _filled_order_ids(self) -> set[str]:
        ids: set[str] = set()
        for e in self.store.query_events(event_type="order_filled", limit=None):
            payload = json.loads(e.get("payload_json", "{}"))
            if payload.get("order_id"):
                ids.add(str(payload["order_id"]))
        return ids

    def _cancelled_order_ids(self) -> set[str]:
        ids: set[str] = set()
        for e in self.store.query_events(event_type="order_cancelled", limit=None):
            payload = json.loads(e.get("payload_json", "{}"))
            if payload.get("order_id"):
                ids.add(str(payload["order_id"]))
        return ids

    @_locked
    def expire_stale_entries(self) -> list[dict]:
        """Back out of unfilled entries that timed out or broke their trigger.

        This is the disciplined other half of auto-arming: once a setup is
        recognized we enter with conviction, but if the fill never comes (the
        move didn't follow through) we don't sit there forever — we cancel on a
        time box or when price falls back through the entry, freeing risk
        budget and the concurrent-position slot for the next setup.

        Safe to call as often as you like: the price-break check uses the live
        ``price_provider`` each call, while the timeout is wall-clock based, so
        running this every few seconds reacts fast without shortening the
        timeout window.
        """
        if not self._armed:
            return []

        filled = self._filled_order_ids()
        cancelled = self._cancelled_order_ids()
        # broker truth: never cancel an entry whose position is actually open
        # (cancelling its bracket parent would strip the protective stop/TP).
        # Fall back to the synced snapshot if the broker can't be read.
        broker_held = self._broker_held_symbols()
        if broker_held is None:
            broker_held = self._held_symbols()
        invalidate_pct = self.settings.entry_invalidate_pct
        timeout_bars = self.settings.entry_timeout_bars
        now = self._now()
        actions: list[dict] = []

        for order_id in list(self._armed):
            armed = self._armed[order_id]
            symbol = armed["symbol"]

            # already resolved at the broker -> stop tracking
            if order_id in filled:
                self._armed.pop(order_id, None)
                continue
            if order_id in cancelled:
                self._armed.pop(order_id, None)
                self._requested_symbols.discard(symbol)
                continue
            # FILLED at the broker (position open) -> stop tracking, NEVER cancel
            # (this is the naked-stop fix: cancelling here killed the stop leg).
            if symbol in broker_held:
                self._armed.pop(order_id, None)
                continue

            armed["checks"] += 1
            reason = None

            # (a) price-break invalidation: traded back below the entry trigger.
            # Uses the live last-trade price, so this reacts tick-by-tick rather
            # than waiting for a bar to close.
            if invalidate_pct >= 0 and self.price_provider is not None:
                try:
                    last = self.price_provider(symbol)
                except Exception:  # noqa: BLE001
                    last = None
                if last is not None:
                    threshold = armed["entry_price"] * (1.0 - invalidate_pct)
                    if float(last) < threshold:
                        reason = (
                            f"entry invalidated: {float(last):.4f} < trigger "
                            f"{threshold:.4f}"
                        )

            # (b) time box: unfilled for too long (wall-clock minutes ~ bars)
            if reason is None and timeout_bars > 0:
                elapsed_min = (now - armed["armed_at"]).total_seconds() / 60.0
                if elapsed_min >= timeout_bars:
                    reason = f"entry timed out: unfilled {elapsed_min:.1f} min"

            if reason is not None:
                self.executor.cancel_entry(
                    order_id, armed.get("broker_order_id"), symbol, reason
                )
                self.store.emit(
                    RiskRuleTriggeredEvent(
                        timestamp=datetime.now(),
                        mode=self.mode,
                        correlation_id=self.session_id,
                        message=f"Backed out of {symbol} entry — {reason}",
                        rule_type="entry_backout",
                        rule_value=float(timeout_bars),
                        current_state={
                            "symbol": symbol,
                            "order_id": order_id,
                            "checks": armed["checks"],
                        },
                        action_taken="cancelled_unfilled_entry",
                    )
                )
                self._armed.pop(order_id, None)
                self._requested_symbols.discard(symbol)
                actions.append({"order_id": order_id, "symbol": symbol, "reason": reason})

        return actions

    @_locked
    def tick(self) -> dict:
        """One execution pass: expire stale entries, request approvals, auto-execute."""
        backed_out = self.expire_stale_entries()
        requested = self.request_approvals_for_ready_signals()
        auto = self.process_auto_approvals() if self.settings.auto_approve else []
        return {
            "approvals_requested": requested,
            "auto_executed": auto,
            "backed_out": backed_out,
        }

    # -- helpers --------------------------------------------------------------

    def _find_pending(self, order_id: str) -> dict | None:
        for entry in query_approval_queue(self.store):
            if str(entry.get("order_id")) == str(order_id):
                return entry
        return None
