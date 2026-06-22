"""Alpaca paper executor + account sync tests (mocked client)."""

import json

import pytest

from alpaca_paper.client import AlpacaApiError
from alpaca_paper.execution import AlpacaPaperExecutor, ExecutionRequest
from alpaca_paper.sync import AlpacaPaperSync
from storage.event_store import EventStore
from storage.projections import (
    query_account_orders_snapshot,
    query_account_positions_snapshot,
    query_account_summary_snapshot,
    query_session_pnl,
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


# --- position-close reconciliation (trade journal) ---------------------------

class ProgClient:
    """A client whose positions/orders we change between sync cycles to
    simulate a position opening then closing."""
    def __init__(self):
        self.positions = []
        self.orders = []
    def get_account(self):
        return {"account_number": "PA", "equity": "100000", "cash": "50000", "buying_power": "100000"}
    def get_positions(self):
        return self.positions
    def get_orders(self, status="all", limit=100, nested=False):
        return self.orders


def _long(sym, entry, cur):
    return {"symbol": sym, "qty": "10", "avg_entry_price": str(entry),
            "current_price": str(cur), "unrealized_pl": "0", "side": "long"}

def _order(oid, sym, side, otype, status, px, *, stop=None, at="2026-06-22T14:00:00Z"):
    return {"id": oid, "symbol": sym, "side": side, "qty": "10", "filled_qty": "10",
            "type": otype, "status": status, "stop_price": (str(stop) if stop else None),
            "filled_avg_price": (str(px) if px is not None else None), "submitted_at": at}


def _closed(store):
    return [json.loads(e["payload_json"]) for e in store.query_events(event_type="position_closed")]


def test_reconcile_emits_close_on_stop_fill(store):
    c = ProgClient()
    sync = AlpacaPaperSync(store, client=c, session_id="t")
    # cycle 1: GOOD opens, only the entry fill present
    c.positions = [_long("GOOD", 13.90, 14.10)]
    c.orders = [_order("buy-1", "GOOD", "buy", "market", "filled", 13.90, at="2026-06-22T13:30:00Z")]
    sync.sync_all()
    assert _closed(store) == []
    # cycle 2: GOOD gone, its bracket STOP leg filled at 13.45
    c.positions = []
    c.orders.append(_order("stop-1", "GOOD", "sell", "stop", "filled", 13.45, stop=13.45))
    sync.sync_all()
    rows = _closed(store)
    assert len(rows) == 1
    p = rows[0]
    assert (p["symbol"], p["exit_price"], p["exit_reason"]) == ("GOOD", 13.45, "stop_loss")
    assert p["realized_pnl"] == -4.5            # (13.45 - 13.90) * 10
    assert p["entry_price"] == 13.90 and p["stop_loss_price"] == 13.45
    # the trade journal now populates
    pnl = query_session_pnl(store)
    assert pnl["closed_trades"] == 1 and pnl["losses"] == 1
    assert pnl["avg_r_multiple"] == -1.0        # (13.45-13.90)/(13.90-13.45)


def test_reconcile_take_profit_is_a_win(store):
    c = ProgClient()
    sync = AlpacaPaperSync(store, client=c, session_id="t")
    c.positions = [_long("WIN", 10.0, 10.0)]
    c.orders = [_order("buy", "WIN", "buy", "market", "filled", 10.0, at="2026-06-22T13:30:00Z")]
    sync.sync_all()
    c.positions = []
    c.orders.append(_order("tp", "WIN", "sell", "limit", "filled", 11.0))
    sync.sync_all()
    p = _closed(store)[0]
    assert p["exit_reason"] == "take_profit" and p["realized_pnl"] == 10.0
    pnl = query_session_pnl(store)
    assert pnl["wins"] == 1 and pnl["win_rate"] == 1.0


def test_reconcile_no_emit_while_open(store):
    c = ProgClient()
    sync = AlpacaPaperSync(store, client=c, session_id="t")
    c.positions = [_long("HOLD", 5.0, 5.2)]
    sync.sync_all(); sync.sync_all()           # still open across cycles
    assert _closed(store) == []


def test_reconcile_fallback_to_last_mark_without_fill(store):
    c = ProgClient()
    sync = AlpacaPaperSync(store, client=c, session_id="t")
    c.positions = [_long("GAP", 4.0, 4.30)]    # last mark 4.30
    sync.sync_all()
    c.positions = []
    c.orders = []                               # no exit fill visible yet
    sync.sync_all()
    p = _closed(store)[0]
    assert p["exit_reason"] == "closed" and p["exit_price"] == 4.30
    assert p["realized_pnl"] == 3.0             # (4.30 - 4.0) * 10


def test_reconcile_seeds_from_persisted_snapshot_after_restart(store):
    # first process records GOOD open, then "crashes"
    c = ProgClient()
    s1 = AlpacaPaperSync(store, client=c, session_id="t")
    c.positions = [_long("REST", 2.0, 2.1)]
    s1.sync_all()
    # NEW sync instance (fresh in-memory baseline) sees GOOD already gone
    c.positions = []
    c.orders = [_order("x", "REST", "sell", "market", "filled", 2.5)]
    s2 = AlpacaPaperSync(store, client=c, session_id="t")
    s2.sync_all()
    rows = _closed(store)
    assert len(rows) == 1 and rows[0]["symbol"] == "REST"
    assert rows[0]["realized_pnl"] == 5.0       # (2.5 - 2.0) * 10
