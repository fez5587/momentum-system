"""Armed-trigger fast path: opening range, the trigger book, and live-fire.

Locks in the "don't miss the move" mechanics: the opening range is computed
once it's complete; a qualified trigger arms and fires only when live price
crosses it; fired names are pinned across re-ranking; and submit_breakout_now
honours the same risk gates as the slow path while placing a marketable order.
"""

import pandas as pd

from alpaca_paper.execution import AlpacaPaperExecutor
from runtime.triggers import (
    ARMED,
    FILLED,
    FIRED,
    WAITING,
    WEAK,
    ArmedTriggerBook,
)
from storage.event_store import EventStore
from strategy.evaluation.structure import opening_range
from trading_execution import ExecutionSettings, TradingExecutionService


def _bars(highs_lows):
    rows = []
    base = pd.Timestamp("2026-06-17 13:30:00")  # 09:30 ET in UTC-naive
    for i, (h, l) in enumerate(highs_lows):
        rows.append({
            "timestamp": base + pd.Timedelta(minutes=i),
            "open": l, "high": h, "low": l, "close": h, "volume": 1000,
            "is_regular_hours": True,
        })
    return pd.DataFrame(rows)


# -- opening range --------------------------------------------------------

def test_opening_range_incomplete_until_n_bars():
    hi, lo, complete = opening_range(_bars([(10, 9.5), (10.2, 9.8)]), orb_bars=5)
    assert complete is False and hi is None and lo is None


def test_opening_range_high_low():
    bars = _bars([(10, 9.5), (10.5, 9.8), (10.3, 9.9), (10.1, 9.7), (10.6, 9.6),
                  (11.0, 10.0)])
    hi, lo, complete = opening_range(bars, orb_bars=5)
    assert complete is True
    assert hi == 10.6   # max high of first 5
    assert lo == 9.5    # min low of first 5


# -- trigger book ---------------------------------------------------------

def _cand(sym, gap=5.0, rvol=3.0, trig=10.0, stop=9.5, rng=0.05, complete=True):
    return {"symbol": sym, "gap_pct": gap, "rvol": rvol, "trigger": trig,
            "stop": stop, "range_pct": rng, "complete": complete}


def test_book_arms_when_complete_and_qualified():
    b = ArmedTriggerBook(gap_min=3, rvol_min=2)
    b.arm([_cand("AAA")])
    assert b.triggers["AAA"].state == ARMED


def test_book_waiting_until_range_complete():
    b = ArmedTriggerBook()
    b.arm([_cand("AAA", complete=False, trig=None, stop=None)])
    assert b.triggers["AAA"].state == WAITING


def test_book_weak_below_thresholds():
    b = ArmedTriggerBook(gap_min=5, rvol_min=3)
    b.arm([_cand("AAA", gap=2, rvol=1)])
    assert b.triggers["AAA"].state == WEAK


def test_book_weak_below_min_dollar_vol():
    """A thin name (low recent $-volume) is too illiquid to fire even if gap/rvol
    qualify — it would fill at the top of a one-bar spike and reverse."""
    b = ArmedTriggerBook(gap_min=3, rvol_min=2, min_dollar_vol=10_000)
    thin = _cand("AAA"); thin["dollar_vol"] = 4_000
    b.arm([thin])
    assert b.triggers["AAA"].state == WEAK
    liquid = _cand("BBB"); liquid["dollar_vol"] = 80_000
    b.arm([liquid])
    assert b.triggers["BBB"].state == ARMED


def test_book_caps_at_max_armed():
    b = ArmedTriggerBook(max_armed=2)
    b.arm([_cand(f"S{i}") for i in range(5)])
    assert len([t for t in b.triggers.values() if t.state != FIRED]) == 2


def test_fires_only_when_price_crosses():
    b = ArmedTriggerBook(gap_min=3, rvol_min=2)
    b.arm([_cand("AAA", trig=10.0)])
    b.update_price("AAA", 9.9)
    assert b.fires() == []
    b.update_price("AAA", 10.01)
    assert [t.symbol for t in b.fires()] == ["AAA"]


def test_fire_blocked_when_price_is_stale():
    """A stalled feed must not fire on a frozen quote — fires() requires the
    price to be fresher than max_price_age_s."""
    b = ArmedTriggerBook(gap_min=3, rvol_min=2, max_price_age_s=5)
    b.arm([_cand("AAA", trig=10.0)])
    b.update_price("AAA", 10.5)                 # fresh cross -> fires
    assert [t.symbol for t in b.fires()] == ["AAA"]
    b.triggers["AAA"].price_ts -= 10            # age the price past the window
    assert b.fires() == []                      # stale -> no fire
    b.update_price("AAA", 10.5)                 # fresh again -> fires
    assert [t.symbol for t in b.fires()] == ["AAA"]
    b.update_price("AAA", None)                 # failed fetch clears the price
    assert b.fires() == [] and b.triggers["AAA"].price is None


def test_fired_state_pinned_across_rearm():
    b = ArmedTriggerBook(max_armed=2, gap_min=3, rvol_min=2)
    b.arm([_cand("AAA"), _cand("BBB")])
    b.mark_fired("AAA")
    b.arm([_cand("CCC"), _cand("DDD")])  # new ranking without AAA
    assert "AAA" in b.triggers and b.triggers["AAA"].state == FIRED


def test_mark_filled_promotes_held():
    b = ArmedTriggerBook(gap_min=3, rvol_min=2)
    b.arm([_cand("AAA")])
    b.mark_filled({"AAA"})
    assert b.triggers["AAA"].state == FILLED


# -- live fire (submit_breakout_now) --------------------------------------

class _FakeClient:
    def __init__(self, resp=None, account=None, positions=None):
        self.resp = resp or {"id": "o1", "status": "new", "filled_qty": "0"}
        self.account = account or {"equity": "100000", "last_equity": "100000"}
        self._positions = positions or []
        self.last = None
        self.closed = []
        self.canceled = []

    def submit_order(self, **kw):
        self.last = kw
        return self.resp

    def cancel_order(self, order_id):
        self.canceled.append(order_id)

    def get_account(self):
        return self.account

    def get_positions(self, fresh=False):
        return self._positions

    def close_position(self, symbol):
        self.closed.append(symbol)
        return {"id": "c"}


def _svc(client):
    store = EventStore(":memory:")
    svc = TradingExecutionService(
        store,
        executor=AlpacaPaperExecutor(store, client=client),
        settings=ExecutionSettings(auto_approve=True, max_daily_loss_pct=0.5),
        session_id="t",
    )
    return store, svc


def test_breakout_now_submits_marketable_limit():
    client = _FakeClient()
    store, svc = _svc(client)
    res = svc.submit_breakout_now("AAA", trigger=10.0, stop=9.5, last_price=10.0)
    assert res["ok"] is True
    assert client.last["order_type"] == "limit"
    assert client.last["limit_price"] >= 10.0        # capped ABOVE the trigger
    assert store.query_events(event_type="order_approved", limit=None)


def test_breakout_now_blocks_when_halted():
    # equity -60% vs prior close, breaker at 50% -> halt
    client = _FakeClient(account={"equity": "40000", "last_equity": "100000"})
    store, svc = _svc(client)
    res = svc.submit_breakout_now("AAA", 10.0, 9.5, last_price=10.0)
    assert res["ok"] is False and res["skipped"] == "halted"


def test_breakout_now_dedups_second_attempt():
    client = _FakeClient()
    store, svc = _svc(client)
    svc.submit_breakout_now("AAA", 10.0, 9.5, last_price=10.0)
    res2 = svc.submit_breakout_now("AAA", 10.0, 9.5, last_price=10.0)
    assert res2["ok"] is False and res2["skipped"] == "already_active"


def test_breakout_now_blocks_when_gross_notional_full():
    """Portfolio gross-notional cap: when open positions already fill the budget,
    a new entry is blocked instead of piling the book toward ~100% gross."""
    # equity 100k, cap 0.60 -> 60k budget; an open position worth 60k exhausts it
    client = _FakeClient(positions=[{"symbol": "HELD", "qty": "1",
                                     "market_value": "60000"}])
    store, svc = _svc(client)
    res = svc.submit_breakout_now("AAA", trigger=10.0, stop=9.5, last_price=10.0)
    assert res["ok"] is False and res["skipped"] == "gross_notional_cap"


def test_breakout_now_rejects_bad_geometry():
    store, svc = _svc(_FakeClient())
    res = svc.submit_breakout_now("AAA", 10.0, 10.5, last_price=10.0)  # stop > entry
    assert res["ok"] is False and res["skipped"] == "bad_geometry"


def test_guard_never_cancels_a_filled_position():
    """The naked-stop fix: a stale-looking armed entry whose position is actually
    OPEN at the broker must NOT be cancelled (that would strip its stop/TP)."""
    from datetime import datetime, timedelta
    store = EventStore(":memory:")
    client = _FakeClient(positions=[{"symbol": "AAA", "qty": "100"}])  # AAA filled
    svc = TradingExecutionService(
        store, executor=AlpacaPaperExecutor(store, client=client),
        settings=ExecutionSettings(entry_timeout_bars=1, entry_invalidate_pct=0.0,
                                   max_daily_loss_pct=0.5),
        session_id="t", price_provider=lambda s: 0.5,  # below trigger -> would invalidate
    )
    # an entry that LOOKS stale (armed 5 min ago, price below trigger)
    svc._armed["o1"] = {"symbol": "AAA", "entry_price": 1.0, "broker_order_id": "b1",
                        "armed_at": datetime.now() - timedelta(minutes=5), "checks": 0}
    backed = svc.expire_stale_entries()
    assert backed == []              # not backed out
    assert client.canceled == []     # stop/TP legs NOT stripped
    assert "o1" not in svc._armed    # but we stop tracking the filled entry


def test_guard_grace_window_protects_just_armed_entry():
    """A marketable entry that just armed (still filling) must NOT be cancelled
    even if price dipped below the trigger — cancelling strips its bracket."""
    from datetime import datetime
    store = EventStore(":memory:")
    client = _FakeClient()  # no positions reported yet (fill not reflected)
    svc = TradingExecutionService(
        store, executor=AlpacaPaperExecutor(store, client=client),
        settings=ExecutionSettings(entry_invalidate_pct=0.0, entry_grace_seconds=5,
                                   max_daily_loss_pct=0.5),
        session_id="t", price_provider=lambda s: 0.5,  # well below trigger
    )
    svc._armed["o1"] = {"symbol": "AAA", "entry_price": 1.0, "broker_order_id": "b1",
                        "armed_at": datetime.now(), "checks": 0}  # armed just now
    assert svc.expire_stale_entries() == []     # grace -> not cancelled
    assert "o1" in svc._armed and client.canceled == []
