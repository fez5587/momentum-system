"""Live exit manager: ratchet the stop UP via replace (never cancel = never naked)."""

import pandas as pd

from runtime.exit_manager import LiveExitManager
from storage.event_store import EventStore
from strategy.exits import ExitConfig, TRAIL_PRIOR_LOW


class _FakeBroker:
    def __init__(self, positions, stop_leg, market_open=True):
        self._positions = positions
        self._stop_leg = stop_leg
        self.market_open = market_open
        self.replaced = []
        self.closed = []
        self.canceled = []
        self.submitted = []

    def get_positions(self):
        return self._positions

    def get_orders(self, status="open", limit=200, nested=True, symbols=None):
        return [self._stop_leg]

    def get_clock(self):
        return {"is_open": self.market_open}

    def submit_order(self, symbol, qty, side="buy", order_type="market",
                     stop_price=None, time_in_force="day", **kw):
        self.submitted.append({"symbol": symbol, "qty": qty, "side": side,
                               "type": order_type, "stop_price": stop_price})
        return {"id": "s"}

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


class _NakedBroker(_FakeBroker):
    def get_orders(self, status="open", limit=200, nested=True, symbols=None):
        # naked until a protective stop is rested, then reflect it (like the real
        # broker) so the NEXT pass sees the stop and stops re-resting/enforcing.
        return [{"id": "rested", "symbol": o["symbol"], "type": o["type"],
                 "side": o["side"], "stop_price": str(o["stop_price"]), "status": "accepted"}
                for o in self.submitted]


def test_naked_position_flattened_after_grace_when_market_open():
    """A held position with NO live protective stop (bracket leg never attached
    or got stripped) is the NIVF case — flatten it after grace WHEN THE MARKET IS OPEN
    (a market exit can fill)."""
    store = EventStore(":memory:")
    pos = [{"symbol": "AAA", "avg_entry_price": "10.0", "current_price": "10.1", "qty": "100"}]
    broker = _NakedBroker(pos, None, market_open=True)
    mgr = LiveExitManager(
        broker, store, lambda s: _bars([(10.2, 10.0, 10.1)]),
        cfg=ExitConfig(enforce_stop_grace_passes=2, catastrophe_pct=0.10),
        session_id="t",
    )
    mgr.manage()                         # pass 1: naked but within grace
    assert broker.closed == []
    mgr.manage()                         # pass 2: grace reached -> flatten
    assert broker.closed == [("AAA", None)]
    assert broker.submitted == []        # flattened, not rest-a-stop


def test_naked_position_rests_stop_when_market_closed():
    """When the market is CLOSED a market flatten can't fill (it just churns every
    pass), so enforcement RESTS a protective stop instead — at breakeven when in
    profit. The next pass would see the stop and stop enforcing."""
    store = EventStore(":memory:")
    pos = [{"symbol": "AAA", "avg_entry_price": "10.0", "current_price": "10.4", "qty": "100"}]
    broker = _NakedBroker(pos, None, market_open=False)
    mgr = LiveExitManager(
        broker, store, lambda s: _bars([(10.5, 10.3, 10.4)]),
        cfg=ExitConfig(enforce_stop_grace_passes=2, catastrophe_pct=0.10),
        session_id="t",
    )
    mgr.manage(); mgr.manage()           # grace reached while CLOSED
    assert broker.closed == []           # NOT flattened (would never fill)
    assert len(broker.submitted) == 1    # rested a protective stop instead
    o = broker.submitted[0]
    assert o["side"] == "sell" and o["type"] == "stop" and o["qty"] == 100
    assert o["stop_price"] == 10.0       # breakeven (position is in profit)


def test_naked_underwater_rests_catastrophe_stop_when_closed():
    """Closed + naked + UNDERWATER: a sell-stop can't sit above the last price, so
    enforcement rests it a catastrophe distance BELOW current, not at breakeven."""
    store = EventStore(":memory:")
    pos = [{"symbol": "AAA", "avg_entry_price": "10.0", "current_price": "9.0", "qty": "100"}]
    broker = _NakedBroker(pos, None, market_open=False)
    mgr = LiveExitManager(
        broker, store, lambda s: _bars([(9.1, 8.9, 9.0)]),
        cfg=ExitConfig(enforce_stop_grace_passes=2, catastrophe_pct=0.10),
        session_id="t",
    )
    mgr.manage(); mgr.manage()
    assert broker.closed == [] and len(broker.submitted) == 1
    assert broker.submitted[0]["stop_price"] == 8.1   # 9.0 * (1 - 0.10), below market


def test_breakeven_position_trails_up_on_synthetic_r():
    """A position found already at BREAKEVEN (stop == entry, original R lost) must still
    trail UP as it rises — on a synthetic R — instead of sitting frozen at breakeven."""
    store = EventStore(":memory:")
    pos = [{"symbol": "AAA", "avg_entry_price": "10.0", "current_price": "11.0", "qty": "100"}]
    leg = {"id": "be", "symbol": "AAA", "type": "stop", "side": "sell",
           "stop_price": "10.0", "status": "accepted"}    # stop AT entry = breakeven
    broker = _FakeBroker(pos, leg)
    bars = _bars([(10.2, 10.0, 10.1), (10.6, 10.2, 10.5), (11.0, 10.6, 11.0)])  # +10%
    mgr = LiveExitManager(
        broker, store, lambda s: bars,
        cfg=ExitConfig(trail_r_step=0.25, breakeven_at_pct=0.05, default_trail_r_pct=0.10),
        session_id="t",
    )
    mgr.manage()
    assert broker.replaced, "breakeven position should trail up, not freeze"
    new_stop = broker.replaced[-1][1]
    assert 10.0 < new_stop < 11.0          # ratcheted ABOVE breakeven, below market


def test_no_trail_attempt_when_market_closed():
    """Trailing runs only while the session is OPEN — after-hours there are no new highs
    and a resting GTC stop often isn't replaceable, so the manager leaves the stop as-is."""
    store = EventStore(":memory:")
    pos = [{"symbol": "AAA", "avg_entry_price": "10.0", "current_price": "11.0", "qty": "100"}]
    leg = {"id": "be", "symbol": "AAA", "type": "stop", "side": "sell",
           "stop_price": "10.0", "status": "accepted"}
    broker = _FakeBroker(pos, leg, market_open=False)
    mgr = LiveExitManager(
        broker, store, lambda s: _bars([(11.0, 10.6, 11.0)]),
        cfg=ExitConfig(trail_r_step=0.25, default_trail_r_pct=0.10), session_id="t",
    )
    mgr.manage()
    assert broker.replaced == []           # closed -> no trail attempt (no churn)


def test_breakeven_position_reanchors_to_opening_range():
    """A carried breakeven position re-anchors its +0.25R trail to TODAY's opening range
    (ref = open, R = first-5-bar high-low) and ladders the stop from the open — locking in
    TIGHTER than the wide entry-based synthetic R would."""
    store = EventStore(":memory:")
    pos = [{"symbol": "AAA", "avg_entry_price": "10.0", "current_price": "10.6", "qty": "100"}]
    leg = {"id": "be", "symbol": "AAA", "type": "stop", "side": "sell",
           "stop_price": "10.0", "status": "accepted"}
    broker = _FakeBroker(pos, leg)
    # opening range (first 5 bars) ~10.0–10.3 (R≈0.3), then runs to 10.6
    bars = _bars([(10.1, 10.0, 10.05), (10.2, 10.05, 10.15), (10.3, 10.1, 10.25),
                  (10.25, 10.15, 10.2), (10.3, 10.2, 10.28), (10.5, 10.3, 10.45),
                  (10.6, 10.45, 10.6)])
    mgr = LiveExitManager(
        broker, store, lambda s: bars,
        cfg=ExitConfig(trail_r_step=0.25, default_trail_r_pct=0.10), session_id="t",
    )
    mgr.manage()
    assert broker.replaced, "should re-anchor + trail from the opening range"
    new_stop = broker.replaced[-1][1]
    assert 10.0 < new_stop < 10.6      # above breakeven, below market
    assert new_stop > 10.25            # tighter than the entry-synthetic R (~10.25) -> OR-anchored
    assert "AAA" in mgr._open_anchored


def test_breakeven_position_frozen_when_disabled():
    """default_trail_r_pct=0 keeps the old behaviour — a breakeven stop is left frozen."""
    store = EventStore(":memory:")
    pos = [{"symbol": "AAA", "avg_entry_price": "10.0", "current_price": "11.0", "qty": "100"}]
    leg = {"id": "be", "symbol": "AAA", "type": "stop", "side": "sell",
           "stop_price": "10.0", "status": "accepted"}
    broker = _FakeBroker(pos, leg)
    mgr = LiveExitManager(
        broker, store, lambda s: _bars([(11.0, 10.6, 11.0)]),
        cfg=ExitConfig(trail_r_step=0.25, default_trail_r_pct=0.0),
        session_id="t",
    )
    mgr.manage()
    assert broker.replaced == []           # frozen at breakeven


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
