"""Tests for the interactive-dashboard projections."""

from datetime import datetime, timedelta

import pytest

from storage.event_schema import (
    AccountPositionsUpdatedEvent,
    CriteriaEvaluatedEvent,
    EventMode,
    OrderFilledEvent,
    OrderSubmittedEvent,
    PositionClosedEvent,
)
from storage.event_store import EventStore
from storage.projections import (
    CRITERIA_LABELS,
    query_fills_feed,
    query_session_pnl,
    query_symbol_criteria,
)

T0 = datetime(2026, 6, 11, 9, 45)


@pytest.fixture
def store():
    s = EventStore(":memory:")
    yield s
    s.close()


def test_symbol_criteria_breakdown(store):
    store.emit(
        CriteriaEvaluatedEvent(
            timestamp=T0, mode=EventMode.PAPER, message="c", symbol="GOOD",
            criteria_results={
                "passed": ["gap", "relative_volume", "vwap", "breakout"],
                "failed": ["impulse", "pullback"],
            },
            total_criteria=9, passed_criteria=4, success_score_pct=44.0,
        )
    )
    result = query_symbol_criteria(store, "GOOD")
    assert result["symbol"] == "GOOD"
    assert result["score"] == 44.0
    by_key = {c["key"]: c for c in result["criteria"]}
    # every known criterion is represented, in canonical order
    assert [c["key"] for c in result["criteria"]] == list(CRITERIA_LABELS)
    assert by_key["gap"]["passed"] is True
    assert by_key["impulse"]["passed"] is False
    # a criterion neither passed nor failed (not evaluated) is None
    assert by_key["candle_quality"]["passed"] is None


def test_symbol_criteria_uses_latest_evaluation(store):
    for score, passed in [(20.0, ["gap"]), (80.0, ["gap", "vwap", "breakout"])]:
        store.emit(
            CriteriaEvaluatedEvent(
                timestamp=T0, mode=EventMode.PAPER, message="c", symbol="GOOD",
                criteria_results={"passed": passed, "failed": []},
                total_criteria=9, passed_criteria=len(passed), success_score_pct=score,
            )
        )
    result = query_symbol_criteria(store, "GOOD")
    assert result["score"] == 80.0


def test_symbol_criteria_empty_when_unseen(store):
    result = query_symbol_criteria(store, "NOPE")
    assert result["criteria"] == []
    assert result["score"] is None


def test_fills_feed_orders_newest_first(store):
    store.emit(
        OrderSubmittedEvent(
            timestamp=T0, mode=EventMode.PAPER, message="s", order_id="o1",
            symbol="AAA", side="buy", quantity=10, price=5.0,
        )
    )
    store.emit(
        OrderFilledEvent(
            timestamp=T0 + timedelta(minutes=1), mode=EventMode.PAPER, message="f",
            order_id="o1", symbol="AAA", fill_price=5.02, fill_quantity=10,
        )
    )
    feed = query_fills_feed(store)
    assert feed[0]["kind"] == "order_filled"
    assert feed[0]["price"] == 5.02
    assert feed[-1]["kind"] == "order_submitted"


def test_session_pnl_realized_and_unrealized(store):
    # two closed trades: one win, one loss
    store.emit(
        PositionClosedEvent(
            timestamp=T0, mode=EventMode.PAPER, message="win", position_id="p1",
            symbol="AAA", exit_price=11.0, exit_reason="target", realized_pnl=150.0,
        )
    )
    store.emit(
        PositionClosedEvent(
            timestamp=T0 + timedelta(minutes=5), mode=EventMode.PAPER, message="loss",
            position_id="p2", symbol="BBB", exit_price=9.0, exit_reason="stop",
            realized_pnl=-60.0,
        )
    )
    # one open position with unrealized gain
    store.emit(
        AccountPositionsUpdatedEvent(
            timestamp=T0 + timedelta(minutes=6), mode=EventMode.PAPER, message="pos",
            broker_name="alpaca_paper", account_id="paper",
            positions=[{"symbol": "CCC", "quantity": 10, "unrealized_pl": 40.0}],
        )
    )
    pnl = query_session_pnl(store)
    assert pnl["realized_pnl"] == 90.0
    assert pnl["unrealized_pnl"] == 40.0
    assert pnl["total_pnl"] == 130.0
    assert pnl["wins"] == 1
    assert pnl["losses"] == 1
    assert pnl["win_rate"] == 0.5
    assert pnl["open_positions"] == 1
    assert pnl["closed_trades"] == 2


def test_session_pnl_empty(store):
    pnl = query_session_pnl(store)
    assert pnl["realized_pnl"] == 0.0
    assert pnl["total_pnl"] == 0.0
    assert pnl["win_rate"] is None
    assert pnl["trades"] == []
