"""Trading execution service tests: signal -> approval -> order -> exit."""

from datetime import datetime, timedelta

import pytest

from alpaca_paper.execution import ExecutionRequest, ExecutionResult
from storage.event_schema import (
    AccountPositionsUpdatedEvent,
    EventMode,
    OrderFilledEvent,
    SignalReadyEvent,
)
from storage.event_store import EventStore
from storage.projections import query_approval_queue
from trading_execution import ExecutionSettings, TradingExecutionService
from trading_mode import TradingModeSettings

T0 = datetime(2026, 6, 11, 9, 45)


class FakeExecutor:
    broker_name = "alpaca_paper"

    def __init__(self, status: str = "accepted"):
        self.executed: list[ExecutionRequest] = []
        self.cancelled: list[dict] = []
        self.ok = True
        self.status = status  # "accepted" = resting/unfilled; "filled" = instant

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.executed.append(request)
        return ExecutionResult(
            ok=self.ok,
            order_id=request.order_id,
            broker_order_id="broker-1" if self.ok else None,
            status=self.status if self.ok else "error",
            error=None if self.ok else "broker down",
        )

    def cancel_entry(self, order_id, broker_order_id, symbol, reason):
        self.cancelled.append(
            {"order_id": order_id, "symbol": symbol, "reason": reason}
        )
        return ExecutionResult(ok=True, order_id=order_id, status="cancelled")


@pytest.fixture
def store():
    s = EventStore(":memory:")
    yield s
    s.close()


class Clock:
    """A controllable clock for deterministic wall-clock timeout tests."""

    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 6, 11, 9, 45)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, minutes: float) -> None:
        self.t += timedelta(minutes=minutes)


def emit_signal(store, symbol="GOOD", entry=14.0, stop=13.45):
    store.emit(
        SignalReadyEvent(
            timestamp=T0,
            mode=EventMode.PAPER,
            correlation_id="t",
            message=f"{symbol} ready",
            symbol=symbol,
            signal_type="bull_flag",
            confidence=0.9,
            signal_data={
                "entry_price": entry,
                "stop_loss_price": stop,
                "quality_score": 0.7,
            },
        )
    )


def emit_positions(store, positions):
    store.emit(
        AccountPositionsUpdatedEvent(
            timestamp=T0,
            mode=EventMode.PAPER,
            message="pos",
            broker_name="alpaca_paper",
            account_id="paper",
            positions=positions,
        )
    )


def make_service(store, executor=None, price_provider=None, now_fn=None, **settings_kw):
    defaults = dict(
        enabled=True, auto_approve=False, max_orders_per_tick=2,
        max_concurrent_positions=3, risk_per_trade_pct=0.01,
        default_equity=100_000.0,
    )
    defaults.update(settings_kw)
    return TradingExecutionService(
        store,
        executor=executor or FakeExecutor(),
        settings=ExecutionSettings(**defaults),
        trading_mode=TradingModeSettings(execution_mode="alpaca_paper"),
        session_id="t",
        price_provider=price_provider,
        now_fn=now_fn,
    )


def test_ready_signal_creates_approval_request(store):
    emit_signal(store)
    service = make_service(store)
    requested = service.request_approvals_for_ready_signals()
    assert len(requested) == 1  # returns order ids
    queue = query_approval_queue(store)
    assert len(queue) == 1
    request = queue[0]["execution_request"]
    # risk sizing: 1% of 100k = $1000 risk / $0.55 per share ≈ 1818 shares
    assert request["quantity"] == int(1000 / 0.55)
    assert request["stop_loss_price"] == 13.45
    # take profit at entry + 2R
    assert request["take_profit_price"] == pytest.approx(14.0 + 2 * 0.55)


def test_manual_approval_executes_order(store):
    emit_signal(store)
    executor = FakeExecutor()
    service = make_service(store, executor)
    service.request_approvals_for_ready_signals()
    order_id = query_approval_queue(store)[0]["order_id"]

    result = service.approve_order(order_id, approved_by="dashboard")
    assert result["ok"]
    assert executor.executed[0].symbol == "GOOD"
    assert store.query_events(event_type="order_approved")
    assert query_approval_queue(store) == []  # no longer pending


def test_rejection_clears_queue_without_executing(store):
    emit_signal(store)
    executor = FakeExecutor()
    service = make_service(store, executor)
    service.request_approvals_for_ready_signals()
    order_id = query_approval_queue(store)[0]["order_id"]

    result = service.reject_order(order_id, reason="too extended")
    assert result["ok"]
    assert executor.executed == []
    assert query_approval_queue(store) == []
    assert store.query_events(event_type="order_rejected")


def test_approving_unknown_order_fails_cleanly(store):
    service = make_service(store)
    assert not service.approve_order("nope")["ok"]
    assert not service.reject_order("nope")["ok"]


def test_auto_approve_tick_executes(store):
    emit_signal(store)
    executor = FakeExecutor()
    service = make_service(store, executor, auto_approve=True)
    out = service.tick()
    assert len(out["approvals_requested"]) == 1
    assert len(out["auto_executed"]) == 1
    assert executor.executed[0].symbol == "GOOD"


def test_no_duplicate_requests_for_same_symbol(store):
    emit_signal(store)
    service = make_service(store)
    assert len(service.request_approvals_for_ready_signals()) == 1
    assert service.request_approvals_for_ready_signals() == []
    assert len(query_approval_queue(store)) == 1


def test_held_symbols_are_skipped(store):
    emit_signal(store)
    emit_positions(store, [{"symbol": "GOOD", "quantity": 10}])
    service = make_service(store)
    assert service.request_approvals_for_ready_signals() == []


def test_max_concurrent_positions_guard(store):
    emit_signal(store, symbol="NEWS")
    emit_positions(
        store,
        [{"symbol": s, "quantity": 1} for s in ("AAA", "BBB", "CCC")],
    )
    service = make_service(store, max_concurrent_positions=3)
    assert service.request_approvals_for_ready_signals() == []
    assert store.query_events(event_type="risk_rule_triggered")


def test_exit_order_closes_position(store):
    emit_positions(store, [{"symbol": "GOOD", "quantity": 10}])
    executor = FakeExecutor()
    service = make_service(store, executor)
    result = service.submit_exit_order("GOOD")
    assert result["ok"]
    exit_request = executor.executed[0]
    assert exit_request.side == "sell"
    assert exit_request.quantity == 10


def test_exit_order_without_position_fails(store):
    service = make_service(store)
    result = service.submit_exit_order("NOPE")
    assert not result["ok"]


# ---------------------------------------------------------------------------
# Auto-arm entry mechanism: tunable reward, entry order type, and back-out
# ---------------------------------------------------------------------------

def test_reward_multiple_is_configurable(store):
    emit_signal(store)  # entry 14.0 stop 13.45 -> risk 0.55
    service = make_service(store, reward_multiple=3.0)
    service.request_approvals_for_ready_signals()
    req = query_approval_queue(store)[0]["execution_request"]
    assert req["take_profit_price"] == pytest.approx(14.0 + 3.0 * 0.55)


def test_entry_order_type_flows_into_request(store):
    emit_signal(store)
    service = make_service(store, entry_order_type="limit")
    service.request_approvals_for_ready_signals()
    req = query_approval_queue(store)[0]["execution_request"]
    assert req["order_type"] == "limit"


def test_unfilled_entry_is_armed_then_times_out(store):
    emit_signal(store)
    executor = FakeExecutor(status="accepted")  # resting limit, never fills
    clock = Clock()
    service = make_service(
        store, executor, auto_approve=True, now_fn=clock,
        entry_timeout_bars=2, entry_invalidate_pct=-1.0,  # disable price-break
    )
    # request + auto-approve -> order is armed, not yet expired
    service.tick()
    assert len(service._armed) == 1
    assert executor.cancelled == []
    # 1 minute later: under the 2-minute window -> still armed
    clock.advance(1)
    assert service.expire_stale_entries() == []
    assert len(service._armed) == 1
    # 2 minutes after arming -> timed out and backed out
    clock.advance(1)
    out = service.expire_stale_entries()
    assert len(out) == 1
    assert len(executor.cancelled) == 1
    assert "timed out" in executor.cancelled[0]["reason"]
    assert service._armed == {}


def test_fast_guard_does_not_shorten_timeout(store):
    """Checking many times a second must not trip the wall-clock timeout early."""
    emit_signal(store)
    executor = FakeExecutor(status="accepted")
    clock = Clock()
    service = make_service(
        store, executor, auto_approve=True, now_fn=clock,
        entry_timeout_bars=2, entry_invalidate_pct=-1.0,
    )
    service.tick()
    # hammer the guard 50 times with no time passing -> no premature back-out
    for _ in range(50):
        assert service.expire_stale_entries() == []
    assert len(service._armed) == 1
    assert executor.cancelled == []


def test_unfilled_entry_invalidated_by_price_break(store):
    emit_signal(store, entry=14.0, stop=13.45)
    executor = FakeExecutor(status="accepted")
    price = {"GOOD": 14.10}  # starts above the trigger
    service = make_service(
        store, executor, auto_approve=True,
        entry_timeout_bars=0,  # disable timeout; isolate price-break
        entry_invalidate_pct=0.0,  # any trade below entry invalidates
        price_provider=lambda sym: price.get(sym),
    )
    service.tick()
    assert len(service._armed) == 1
    # backdate past the fill-grace window so the price-break logic can act
    from datetime import datetime, timedelta
    for a in service._armed.values():
        a["armed_at"] = datetime.now() - timedelta(seconds=30)
    # price holds above entry -> no back-out
    assert service.expire_stale_entries() == []
    # price breaks back below the entry trigger -> cancel
    price["GOOD"] = 13.90
    out = service.expire_stale_entries()
    assert len(out) == 1
    assert "invalidated" in executor.cancelled[0]["reason"]
    assert service._armed == {}


def test_armed_entry_cleared_on_fill_without_cancel(store):
    emit_signal(store)
    executor = FakeExecutor(status="accepted")
    service = make_service(
        store, executor, auto_approve=True, entry_timeout_bars=1,
    )
    service.tick()
    assert len(service._armed) == 1
    order_id = next(iter(service._armed))
    # broker reports the fill via the event store
    store.emit(
        OrderFilledEvent(
            timestamp=T0, mode=EventMode.PAPER, message="fill",
            order_id=order_id, symbol="GOOD", fill_price=14.01, fill_quantity=10,
        )
    )
    out = service.expire_stale_entries()
    # filled -> tracking stops, nothing cancelled even though timeout=1
    assert out == []
    assert executor.cancelled == []
    assert service._armed == {}


def test_backout_frees_symbol_to_resignal(store):
    emit_signal(store)
    executor = FakeExecutor(status="accepted")
    clock = Clock()
    service = make_service(
        store, executor, auto_approve=True, now_fn=clock,
        entry_timeout_bars=1, entry_invalidate_pct=-1.0,
    )
    service.tick()
    symbol = service._armed[next(iter(service._armed))]["symbol"]
    assert symbol in service._requested_symbols
    clock.advance(1)  # reach the 1-minute timeout
    service.expire_stale_entries()
    # after backing out, the symbol is no longer marked as requested
    assert symbol not in service._requested_symbols
