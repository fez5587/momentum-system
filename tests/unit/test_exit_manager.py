"""Live exit manager: ratchet the stop UP via replace (never cancel = never naked)."""

import pandas as pd

from runtime.exit_manager import LiveExitManager
from storage.event_store import EventStore
from strategy.exits import ExitConfig, TRAIL_PRIOR_LOW


class _FakeBroker:
    def __init__(self, positions, stop_leg):
        self._positions = positions
        self._stop_leg = stop_leg
        self.replaced = []
        self.closed = []
        self.canceled = []

    def get_positions(self):
        return self._positions

    def get_orders(self, status="open", limit=200, nested=True, symbols=None):
        return [self._stop_leg]

    def replace_order(self, order_id, stop_price=None, **kw):
        self.replaced.append((order_id, stop_price))
        return {"id": order_id}

    def cancel_order(self, order_id):
        self.canceled.append(order_id)

    def close_position(self, symbol, qty=None, percentage=None):
        self.closed.append((symbol, qty))
        return {"id": "c"}


def _bars(seq):
    base = pd.Timestamp("2026-06-17 13:30:00")
    return pd.DataFrame([
        {"timestamp": base + pd.Timedelta(minutes=i), "high": h, "low": lo, "close": c}
        for i, (h, lo, c) in enumerate(seq)
    ])


def test_manager_ratchets_stop_up_and_never_cancels():
    store = EventStore(":memory:")
    pos = [{"symbol": "AAA", "avg_entry_price": "10.0", "qty": "100"}]
    leg = {"id": "stop1", "symbol": "AAA", "type": "stop", "side": "sell",
           "stop_price": "9.0", "status": "held"}
    broker = _FakeBroker(pos, leg)
    bars = _bars([(10.5, 9.8, 10.4), (11.5, 10.5, 11.4), (12.2, 11.5, 12.0)])
    mgr = LiveExitManager(
        broker, store, lambda s: bars,
        cfg=ExitConfig(breakeven_at_r=1.0, trail_mode=TRAIL_PRIOR_LOW, trail_after_r=1.0),
        session_id="t",
    )
    mgr.manage()
    assert broker.replaced, "expected the stop to be moved up"
    order_id, new_stop = broker.replaced[-1]
    assert order_id == "stop1" and new_stop > 9.0   # ratcheted UP
    assert broker.canceled == []                    # never cancels the protective leg


def test_flatten_cancels_protective_orders_then_closes():
    """A market close is rejected while the bracket legs hold the qty
    (held_for_orders). _flatten must cancel the protective sells FIRST, then
    liquidate — otherwise the trail/first-red exit 403s every pass forever."""
    class _HeldBroker:
        def __init__(self):
            self.canceled = []
            self.closed = []
            self._held = True  # qty locked by the resting protective orders

        def close_position(self, symbol, qty=None, percentage=None):
            if self._held:
                raise RuntimeError("403 insufficient qty available for order")
            self.closed.append(symbol)
            return {"id": "c"}

        def cancel_order(self, order_id):
            self.canceled.append(order_id)
            self._held = False  # releasing the legs frees the shares

    broker = _HeldBroker()
    mgr = LiveExitManager(broker, EventStore(":memory:"), lambda s: None,
                          cfg=ExitConfig(trail_mode=TRAIL_PRIOR_LOW), session_id="t")
    orders = [
        {"symbol": "AAA", "side": "sell", "status": "held", "type": "stop", "id": "stop1"},
        {"symbol": "AAA", "side": "sell", "status": "new", "type": "limit", "id": "tp1"},
        {"symbol": "BBB", "side": "sell", "status": "held", "type": "stop", "id": "other"},
    ]
    mgr._flatten("AAA", orders)
    assert set(broker.canceled) == {"stop1", "tp1"}   # released AAA's qty (not BBB's)
    assert broker.closed == ["AAA"]                    # then liquidated


def test_naked_position_flattened_after_grace():
    """A held position with NO live protective stop (bracket leg never attached
    or got stripped) is the NIVF case — flatten it after the grace passes."""
    store = EventStore(":memory:")
    pos = [{"symbol": "AAA", "avg_entry_price": "10.0", "current_price": "10.1", "qty": "100"}]

    class _NakedBroker(_FakeBroker):
        def get_orders(self, status="open", limit=200, nested=True, symbols=None):
            return []   # no protective stop leg at all -> naked

    broker = _NakedBroker(pos, None)
    mgr = LiveExitManager(
        broker, store, lambda s: _bars([(10.2, 10.0, 10.1)]),
        cfg=ExitConfig(enforce_stop_grace_passes=2, catastrophe_pct=0.10),
        session_id="t",
    )
    mgr.manage()                         # pass 1: naked but within grace
    assert broker.closed == []
    mgr.manage()                         # pass 2: grace reached -> flatten
    assert broker.closed == [("AAA", None)]


def test_position_with_stop_not_flattened_as_naked():
    """A position WITH a live stop must never be naked-flattened."""
    store = EventStore(":memory:")
    pos = [{"symbol": "AAA", "avg_entry_price": "10.0", "current_price": "10.1", "qty": "100"}]
    leg = {"id": "stop1", "symbol": "AAA", "type": "stop", "side": "sell",
           "stop_price": "9.5", "status": "held"}
    broker = _FakeBroker(pos, leg)
    mgr = LiveExitManager(
        broker, store, lambda s: _bars([(10.2, 10.0, 10.1)]),
        cfg=ExitConfig(enforce_stop_grace_passes=2, catastrophe_pct=0.10),
        session_id="t",
    )
    mgr.manage(); mgr.manage(); mgr.manage()
    assert broker.closed == []           # has a stop -> never naked-flattened


def test_manager_noop_for_static_bracket():
    # no active rules -> the broker OCO handles everything; manager does nothing
    broker = _FakeBroker([{"symbol": "AAA", "avg_entry_price": "10", "qty": "100"}],
                         {"id": "s", "symbol": "AAA", "type": "stop", "side": "sell",
                          "stop_price": "9", "status": "held"})
    mgr = LiveExitManager(broker, EventStore(":memory:"), lambda s: _bars([(11, 10, 10.9)]),
                          cfg=ExitConfig(target_r=2.0), session_id="t")
    assert mgr.manage() == []
    assert not broker.replaced and not broker.closed
