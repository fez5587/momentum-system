"""Tests for the interactive-dashboard projections."""

from datetime import datetime, timedelta

import pytest

from storage.event_schema import (
    AccountPositionsUpdatedEvent,
    AccountSummaryUpdatedEvent,
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
    # one open position with unrealized gain — keyed `unrealized_pnl`, exactly as
    # the live AlpacaPaperSync emits it (NOT `unrealized_pl`).
    store.emit(
        AccountPositionsUpdatedEvent(
            timestamp=T0 + timedelta(minutes=6), mode=EventMode.PAPER, message="pos",
            broker_name="alpaca_paper", account_id="paper",
            positions=[{"symbol": "CCC", "quantity": 10, "unrealized_pnl": 40.0}],
        )
    )
    pnl = query_session_pnl(store, for_date="2026-06-11")   # the day these events fall on
    assert pnl["realized_pnl"] == 90.0
    assert pnl["unrealized_pnl"] == 40.0
    assert pnl["total_pnl"] == 130.0
    assert pnl["wins"] == 1
    assert pnl["losses"] == 1
    assert pnl["win_rate"] == 0.5
    assert pnl["open_positions"] == 1
    assert pnl["closed_trades"] == 2


def test_session_pnl_broker_delta_attributes_unrealized(store):
    """Regression for the field-name bug that mislabelled a whole +$330/-$67 day
    as '$263 realized, $0 unrealized'. The positions snapshot stores the gain
    under `unrealized_pnl`; reading `unrealized_pl` returned 0 so the entire
    broker equity-delta fell into the realized bucket. With a broker summary
    present, realized must be (day_delta - unrealized), not the whole delta."""
    # broker day P&L = 92_007.78 - 91_744.94 = +262.84 (no position_closed events)
    store.emit(
        AccountSummaryUpdatedEvent(
            timestamp=T0, mode=EventMode.PAPER, message="acct",
            broker_name="alpaca_paper", account_id="paper", account_desc="Alpaca Paper",
            total_equity=92_007.78, cash_balance=67_101.12, buying_power=305_348.0,
            net_liquidating_value=92_007.78, last_equity=91_744.94,
        )
    )
    # 3 open positions net -66.85 unrealized (the live shape, `unrealized_pnl` key)
    store.emit(
        AccountPositionsUpdatedEvent(
            timestamp=T0 + timedelta(minutes=1), mode=EventMode.PAPER, message="pos",
            broker_name="alpaca_paper", account_id="paper",
            positions=[
                {"symbol": "ATPC", "quantity": 910, "unrealized_pnl": 59.09},
                {"symbol": "NOWL", "quantity": 3662, "unrealized_pnl": -73.24},
                {"symbol": "WPRT", "quantity": 2731, "unrealized_pnl": -52.70},
            ],
        )
    )
    # scope to T0's day so there's no prior-session carry (start-of-day baseline = 0)
    pnl = query_session_pnl(store, for_date="2026-06-11")
    assert pnl["pnl_source"] == "broker"
    assert pnl["total_pnl"] == 262.84           # broker equity delta
    assert pnl["unrealized_pnl"] == -66.85      # attributed to the open positions
    assert pnl["realized_pnl"] == 329.69        # delta - unrealized, NOT the whole delta
    assert pnl["open_positions"] == 3


def test_session_pnl_nets_out_overnight_carry(store):
    """Regression for 'past days don't reflect that day's P&L'. A position held
    overnight carried its full mark-vs-entry into the day; realized = broker_day
    - unrealized then fabricated P&L (a $0 holiday read as +$330/-$330). The
    day's realized must net out the START-OF-DAY unrealized, so only the day's
    own open-position move counts."""
    prev = datetime(2026, 6, 10, 15, 0)   # prior session
    day = datetime(2026, 6, 11, 15, 0)    # the day under review
    # prior session ends with the position -100 underwater
    store.emit(AccountPositionsUpdatedEvent(
        timestamp=prev, mode=EventMode.PAPER, message="pos",
        broker_name="alpaca_paper", account_id="paper",
        positions=[{"symbol": "HOLD", "quantity": 100, "unrealized_pnl": -100.0}]))
    # the review day: equity +50 vs prior close; same position recovers to -40
    store.emit(AccountSummaryUpdatedEvent(
        timestamp=day, mode=EventMode.PAPER, message="acct",
        broker_name="alpaca_paper", account_id="paper", account_desc="Alpaca Paper",
        total_equity=100_050.0, cash_balance=0.0, buying_power=0.0,
        net_liquidating_value=100_050.0, last_equity=100_000.0))
    store.emit(AccountPositionsUpdatedEvent(
        timestamp=day, mode=EventMode.PAPER, message="pos",
        broker_name="alpaca_paper", account_id="paper",
        positions=[{"symbol": "HOLD", "quantity": 100, "unrealized_pnl": -40.0}]))

    pnl = query_session_pnl(store, for_date="2026-06-11")
    assert pnl["total_pnl"] == 50.0             # broker day delta (unchanged)
    assert pnl["unrealized_pnl"] == 60.0        # the day's move: -40 - (-100)
    assert pnl["realized_pnl"] == -10.0         # 50 - 60, NOT 50 - (-40) = 90
    assert pnl["open_unrealized_pnl"] == -40.0  # full open mark still exposed
    # identity holds
    assert pnl["realized_pnl"] + pnl["unrealized_pnl"] == pnl["total_pnl"]


def test_session_pnl_unrealized_derived_when_pnl_missing(store):
    """If a snapshot lacks unrealized_pnl entirely, derive it from
    quantity*(current_price-avg_entry_price) rather than silently reading 0."""
    store.emit(
        AccountPositionsUpdatedEvent(
            timestamp=T0, mode=EventMode.PAPER, message="pos",
            broker_name="alpaca_paper", account_id="paper",
            positions=[{"symbol": "DDD", "quantity": 100,
                        "avg_entry_price": 2.00, "current_price": 2.25}],
        )
    )
    pnl = query_session_pnl(store)
    assert pnl["unrealized_pnl"] == 25.0
    assert pnl["open_positions"] == 1


def test_session_pnl_empty(store):
    pnl = query_session_pnl(store)
    assert pnl["realized_pnl"] == 0.0
    assert pnl["total_pnl"] == 0.0
    assert pnl["win_rate"] is None
    assert pnl["trades"] == []


def test_alltime_score_aggregates_all_days(store):
    from storage.projections import query_alltime_score
    assert query_alltime_score(store)["trades"] == 0      # empty -> zeros
    # day 1: one win
    store.emit(PositionClosedEvent(
        timestamp=datetime(2026, 6, 17, 10, 0), mode=EventMode.PAPER, message="w",
        position_id="a", symbol="AAA", exit_price=11.0, exit_reason="take_profit",
        realized_pnl=100.0, entry_price=10.0, stop_loss_price=9.5, side="buy"))
    # day 2: one win + one loss
    store.emit(PositionClosedEvent(
        timestamp=datetime(2026, 6, 18, 10, 0), mode=EventMode.PAPER, message="w",
        position_id="b", symbol="BBB", exit_price=6.0, exit_reason="market_exit",
        realized_pnl=50.0, entry_price=5.0, side="buy"))
    store.emit(PositionClosedEvent(
        timestamp=datetime(2026, 6, 18, 11, 0), mode=EventMode.PAPER, message="l",
        position_id="c", symbol="CCC", exit_price=9.0, exit_reason="stop_loss",
        realized_pnl=-200.0, entry_price=10.0, stop_loss_price=9.0, side="buy"))
    a = query_alltime_score(store)
    assert a["trades"] == 3 and a["wins"] == 2 and a["losses"] == 1
    assert a["win_rate"] == 0.667
    assert a["total_realized"] == -50.0
    assert a["trading_days"] == 2
    assert a["best_day"] == {"date": "2026-06-17", "pnl": 100.0}
    assert a["worst_day"] == {"date": "2026-06-18", "pnl": -150.0}


def _acct(store, ts, eq, le):
    store.emit(AccountSummaryUpdatedEvent(
        timestamp=ts, mode=EventMode.PAPER, message="acct",
        broker_name="alpaca_paper", account_id="paper", account_desc="Alpaca Paper",
        total_equity=eq, cash_balance=0.0, buying_power=0.0,
        net_liquidating_value=eq, last_equity=le))


def test_daily_performance_curve_trading_days_only():
    from storage.projections import query_daily_performance
    store = EventStore(":memory:")
    # Wed 6/17: +100 day, 1 win
    _acct(store, datetime(2026, 6, 17, 16, 0), 10_100.0, 10_000.0)
    store.emit(PositionClosedEvent(
        timestamp=datetime(2026, 6, 17, 10, 0), mode=EventMode.PAPER, message="w",
        position_id="a", symbol="AAA", exit_price=11.0, exit_reason="take_profit",
        realized_pnl=100.0, entry_price=10.0, side="buy"))
    # Thu 6/18: -200 day, 1W 1L
    _acct(store, datetime(2026, 6, 18, 16, 0), 9_900.0, 10_100.0)
    store.emit(PositionClosedEvent(
        timestamp=datetime(2026, 6, 18, 10, 0), mode=EventMode.PAPER, message="w",
        position_id="b", symbol="BBB", exit_price=6.0, exit_reason="market_exit",
        realized_pnl=50.0, entry_price=5.0, side="buy"))
    store.emit(PositionClosedEvent(
        timestamp=datetime(2026, 6, 18, 11, 0), mode=EventMode.PAPER, message="l",
        position_id="c", symbol="CCC", exit_price=9.0, exit_reason="stop_loss",
        realized_pnl=-150.0, entry_price=10.0, side="buy"))
    # Sat 6/20: flat, no trades -> must be dropped (not a trading day)
    _acct(store, datetime(2026, 6, 20, 16, 0), 9_900.0, 9_900.0)

    days = query_daily_performance(store)
    assert [d["date"] for d in days] == ["2026-06-17", "2026-06-18"]
    assert days[0]["day_pnl"] == 100.0 and days[0]["cum_pnl"] == 100.0 and days[0]["win_rate"] == 1.0
    assert days[1]["day_pnl"] == -200.0 and days[1]["cum_pnl"] == -100.0
    assert days[1]["trades"] == 2 and days[1]["win_rate"] == 0.5
    store.close()
