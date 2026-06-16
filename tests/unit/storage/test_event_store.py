"""Event store ordering, payload round-trip, and projection tests (M2)."""

from datetime import datetime, timedelta

import pytest

from storage.event_schema import (
    AccountPositionsUpdatedEvent,
    AccountSummaryUpdatedEvent,
    EventMode,
    OrderApprovalRequestedEvent,
    OrderApprovedEvent,
    OrderRejectedEvent,
    OrderSubmittedEvent,
    SignalReadyEvent,
    SymbolStateChangedEvent,
)
from storage.event_store import EventStore
from storage.projections import (
    query_account_positions_snapshot,
    query_account_summary_snapshot,
    query_approval_queue,
    query_order_lifecycle_snapshot,
    query_ready_signals_snapshot,
    query_watch_states_snapshot,
)

T0 = datetime(2026, 6, 11, 9, 30)


@pytest.fixture
def store():
    s = EventStore(":memory:")
    yield s
    s.close()


def _signal(symbol="ABCD", ts=T0, entry=5.5, stop=5.2, quality=0.7):
    return SignalReadyEvent(
        timestamp=ts,
        mode=EventMode.PAPER,
        message=f"{symbol} ready",
        symbol=symbol,
        signal_type="bull_flag",
        confidence=0.8,
        signal_data={"entry_price": entry, "stop_loss_price": stop, "quality_score": quality},
    )


def test_emit_and_query_round_trip(store):
    event_id = store.emit(_signal())
    rows = store.query_events(event_type="signal_ready")
    assert len(rows) == 1
    assert rows[0]["id"] == event_id
    import json
    payload = json.loads(rows[0]["payload_json"])
    assert payload["symbol"] == "ABCD"
    assert payload["signal_data"]["entry_price"] == 5.5


def test_query_orders_by_timestamp_ascending(store):
    for i in (3, 1, 2):
        store.emit(_signal(symbol=f"S{i}", ts=T0 + timedelta(minutes=i)))
    rows = store.query_events(event_type="signal_ready")
    times = [r["timestamp"] for r in rows]
    assert times == sorted(times)


def test_query_filters_by_symbol_and_session(store):
    a = _signal(symbol="AAA")
    a.correlation_id = "sess-1"
    b = _signal(symbol="BBB")
    b.correlation_id = "sess-2"
    store.emit(a)
    store.emit(b)
    assert len(store.query_events(symbol="AAA")) == 1
    assert len(store.query_events(session_id="sess-2")) == 1


def test_ready_signals_snapshot_keeps_latest_per_symbol(store):
    store.emit(_signal(symbol="ABCD", ts=T0, entry=5.0))
    store.emit(_signal(symbol="ABCD", ts=T0 + timedelta(minutes=5), entry=6.0))
    snapshot = query_ready_signals_snapshot(store)
    assert len(snapshot) == 1
    assert snapshot[0]["entry_price"] == 6.0
    assert snapshot[0]["stop_loss_price"] == 5.2
    assert snapshot[0]["quality_score"] == 0.7


def _approval(order_id, symbol="ABCD", ts=T0):
    return OrderApprovalRequestedEvent(
        timestamp=ts,
        mode=EventMode.PAPER,
        message="approval requested",
        order_id=order_id,
        symbol=symbol,
        requested_by="execution",
        approval_mode="manual",
        execution_mode="alpaca_paper",
        execution_request={"symbol": symbol, "side": "buy", "quantity": 10},
    )


def test_approval_queue_pending_only(store):
    store.emit(_approval("o-1", "AAA"))
    store.emit(_approval("o-2", "BBB", ts=T0 + timedelta(minutes=1)))
    store.emit(_approval("o-3", "CCC", ts=T0 + timedelta(minutes=2)))
    store.emit(
        OrderApprovedEvent(
            timestamp=T0 + timedelta(minutes=3), mode=EventMode.PAPER,
            message="ok", order_id="o-1", symbol="AAA", approved_by="test",
        )
    )
    store.emit(
        OrderRejectedEvent(
            timestamp=T0 + timedelta(minutes=3), mode=EventMode.PAPER,
            message="no", order_id="o-2", symbol="BBB",
            rejected_by="test", rejection_reason="risk",
        )
    )
    queue = query_approval_queue(store)
    assert [q["order_id"] for q in queue] == ["o-3"]
    assert queue[0]["execution_request"]["quantity"] == 10


def test_order_lifecycle_tracks_status(store):
    store.emit(
        OrderSubmittedEvent(
            timestamp=T0 + timedelta(minutes=1), mode=EventMode.PAPER,
            message="submitted", order_id="o-9", symbol="ZZZZ",
            side="buy", quantity=10, price=5.5,
        )
    )
    lifecycle = {o["order_id"]: o for o in query_order_lifecycle_snapshot(store)}
    assert lifecycle["o-9"]["status"] == "submitted"
    assert lifecycle["o-9"]["quantity"] == 10
    assert lifecycle["o-9"]["side"] == "buy"


def test_watch_states_snapshot(store):
    store.emit(
        SymbolStateChangedEvent(
            timestamp=T0, mode=EventMode.PAPER, message="x",
            symbol="ABCD", previous_state="watching", new_state="ready",
            state_reason="score 85",
        )
    )
    snapshot = query_watch_states_snapshot(store)
    assert snapshot[0]["symbol"] == "ABCD"
    assert snapshot[0]["state"] == "ready"


def test_account_snapshots_latest_per_account(store):
    for equity in (100_000.0, 101_500.0):
        store.emit(
            AccountSummaryUpdatedEvent(
                timestamp=T0, mode=EventMode.PAPER, message="acct",
                broker_name="alpaca_paper", account_id="paper",
                account_desc="Alpaca Paper", total_equity=equity,
                cash_balance=50_000.0, buying_power=200_000.0,
                net_liquidating_value=equity,
            )
        )
    accounts = query_account_summary_snapshot(store)
    assert len(accounts) == 1
    assert accounts[0]["total_equity"] == 101_500.0

    store.emit(
        AccountPositionsUpdatedEvent(
            timestamp=T0, mode=EventMode.PAPER, message="pos",
            broker_name="alpaca_paper", account_id="paper",
            positions=[{"symbol": "ABCD", "quantity": 10}],
        )
    )
    positions = query_account_positions_snapshot(store, broker_name="alpaca_paper")
    assert positions[-1]["positions"][0]["symbol"] == "ABCD"
