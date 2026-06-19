"""Alpaca paper executor + account sync tests (mocked client)."""

import pytest

from alpaca_paper.client import AlpacaApiError
from alpaca_paper.execution import AlpacaPaperExecutor, ExecutionRequest
from alpaca_paper.sync import AlpacaPaperSync
from storage.event_store import EventStore
from storage.projections import (
    query_account_orders_snapshot,
    query_account_positions_snapshot,
    query_account_summary_snapshot,
)


class FakeClient:
    def __init__(self):
        self.submitted = []
        self.fail_with = None

    def submit_order(self, **kw):
        if self.fail_with:
            raise self.fail_with
        self.submitted.append(kw)
        return {"id": "broker-123", "status": "accepted", "filled_avg_price": None}

    def get_account(self):
        return {
            "account_number": "PA-TEST",
            "equity": "100250.50",
            "cash": "60000",
            "buying_power": "200501",
        }

    def get_positions(self):
        return [
            {
                "symbol": "GOOD",
                "qty": "10",
                "avg_entry_price": "13.90",
                "current_price": "14.10",
                "unrealized_pl": "2.0",
            }
        ]

    def get_orders(self, status="all", limit=100, nested=False):
        return [{"id": "broker-123", "symbol": "GOOD", "status": "filled"}]


@pytest.fixture
def store():
    s = EventStore(":memory:")
    yield s
    s.close()


def test_executor_submits_and_emits(store):
    client = FakeClient()
    executor = AlpacaPaperExecutor(store, client=client, session_id="t")
    request = ExecutionRequest(
        symbol="GOOD", side="buy", quantity=10,
        entry_price=14.0, stop_loss_price=13.45,
    )
    result = executor.execute(request)
    assert result.ok
    assert result.broker_order_id == "broker-123"
    assert client.submitted[0]["symbol"] == "GOOD"
    submitted = store.query_events(event_type="order_submitted")
    assert len(submitted) == 1


def test_executor_rejects_zero_quantity(store):
    executor = AlpacaPaperExecutor(store, client=FakeClient(), session_id="t")
    result = executor.execute(ExecutionRequest(symbol="GOOD", side="buy", quantity=0))
    assert not result.ok
    assert "quantity" in (result.error or "")


def test_executor_api_failure_emits_cancelled(store):
    client = FakeClient()
    client.fail_with = AlpacaApiError(403, "forbidden")
    executor = AlpacaPaperExecutor(store, client=client, session_id="t")
    result = executor.execute(ExecutionRequest(symbol="GOOD", side="buy", quantity=5))
    assert not result.ok
    assert store.query_events(event_type="order_cancelled")


def test_execution_request_payload_round_trip():
    request = ExecutionRequest(
        symbol="GOOD", side="buy", quantity=7,
        entry_price=10.0, stop_loss_price=9.5, take_profit_price=11.0,
    )
    clone = ExecutionRequest.from_payload(request.to_payload())
    assert clone.order_id == request.order_id
    assert clone.symbol == "GOOD"
    assert clone.quantity == 7
    assert clone.stop_loss_price == 9.5


def test_sync_all_populates_snapshots(store):
    sync = AlpacaPaperSync(store, client=FakeClient(), session_id="t")
    sync.sync_all()
    accounts = query_account_summary_snapshot(store, broker_name="alpaca_paper")
    assert accounts and accounts[0]["total_equity"] == pytest.approx(100250.50)
    positions = query_account_positions_snapshot(store, broker_name="alpaca_paper")
    assert positions[-1]["positions"][0]["symbol"] == "GOOD"
    orders = query_account_orders_snapshot(store, broker_name="alpaca_paper")
    assert orders[-1]["orders"][0]["broker_order_id"] == "broker-123"


def test_sync_survives_client_failure(store):
    class Down(FakeClient):
        def get_account(self):
            raise AlpacaApiError(500, "down")

        def get_positions(self):
            raise AlpacaApiError(500, "down")

        def get_orders(self, status="all", limit=100, nested=False):
            raise AlpacaApiError(500, "down")

    sync = AlpacaPaperSync(store, client=Down(), session_id="t")
    sync.sync_all()  # must not raise
    assert query_account_summary_snapshot(store) == []
