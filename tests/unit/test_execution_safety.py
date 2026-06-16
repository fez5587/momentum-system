"""Execution/broker safety regressions (from the silent-failure audit).

Locks in: a rejected or id-less order is never reported ok; partial fills are
recorded; and the daily-loss circuit breaker fails CLOSED when equity can't be
read (rather than silently disabling itself).
"""

from alpaca_paper.execution import AlpacaPaperExecutor, ExecutionRequest
from storage.event_store import EventStore
from trading_execution import ExecutionSettings, TradingExecutionService


class _FakeClient:
    def __init__(self, resp=None, account=None, raise_account=False):
        self.resp = resp or {}
        self.account = account or {"equity": "100000", "last_equity": "100000"}
        self.raise_account = raise_account

    def submit_order(self, **kw):
        return self.resp

    def cancel_order(self, order_id):
        pass

    def get_account(self):
        if self.raise_account:
            raise RuntimeError("API down")
        return self.account


def _req(symbol="X"):
    return ExecutionRequest(
        symbol=symbol, side="buy", quantity=10, entry_price=1.0,
        stop_loss_price=0.9, take_profit_price=1.2, order_type="limit",
    )


def test_rejected_order_is_not_ok():
    ex = AlpacaPaperExecutor(
        EventStore(":memory:"),
        client=_FakeClient({"id": "o1", "status": "rejected", "filled_qty": "0"}),
    )
    r = ex.execute(_req())
    assert r.ok is False and r.status == "rejected"


def test_order_with_no_broker_id_is_not_ok():
    ex = AlpacaPaperExecutor(EventStore(":memory:"), client=_FakeClient({"status": "new"}))
    assert ex.execute(_req()).ok is False


def test_partial_fill_is_recorded():
    store = EventStore(":memory:")
    ex = AlpacaPaperExecutor(store, client=_FakeClient(
        {"id": "o2", "status": "partially_filled", "filled_qty": "5", "filled_avg_price": "1.01"}))
    r = ex.execute(_req("Y"))
    assert r.ok is True
    assert len(store.query_events(event_type="order_filled", limit=None)) == 1


def test_breaker_fails_closed_when_equity_unreadable():
    store = EventStore(":memory:")
    svc = TradingExecutionService(
        store,
        executor=AlpacaPaperExecutor(store, client=_FakeClient(raise_account=True)),
        settings=ExecutionSettings(max_daily_loss_pct=0.03),
        session_id="cb",
    )
    assert svc._daily_loss_breach() is False   # 1 failure
    assert svc._daily_loss_breach() is False   # 2 failures
    assert svc._daily_loss_breach() is True    # 3 -> fail CLOSED
    assert svc._halted is True
