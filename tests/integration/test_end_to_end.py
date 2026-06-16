"""End-to-end integration: ingestion -> watcher -> approval -> execution.

Exercises the exact pipeline run_live_paper.py drives, with a fake Alpaca
client, asserting the event-sourced projections the dashboard reads.
"""

from datetime import datetime, timedelta, timezone

import pytest

from alpaca_paper.execution import AlpacaPaperExecutor
from alpaca_paper.sync import AlpacaPaperSync
from research.ingestion.market_data import ingest_daily_history, ingest_live_minute_bars
from research.ingestion.watcher_task import ResearchWatchlistProvider
from runtime.watcher import Watcher, WatcherConfig
from storage.db import get_connection
from storage.event_schema import EventMode
from storage.event_store import EventStore
from storage.projections import (
    query_account_summary_snapshot,
    query_approval_queue,
    query_order_lifecycle_snapshot,
    query_ready_signals_snapshot,
    query_watch_states_snapshot,
)
from trading_execution import ExecutionSettings, TradingExecutionService
from trading_mode import TradingModeSettings

SESSION = datetime(2026, 6, 11, tzinfo=timezone.utc)
SESSION_DATE = SESSION.date()


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def bull_flag_payload(start_utc):
    """Same shape as tests.synthetic.bull_flag_bars, as Alpaca API dicts."""
    specs = [
        (12.50, 12.60, 12.45, 12.55, 60_000),
        (12.55, 12.65, 12.50, 12.60, 55_000),
        (12.60, 12.70, 12.55, 12.65, 50_000),
        (12.65, 12.95, 12.62, 12.92, 120_000),
        (12.92, 13.25, 12.90, 13.20, 150_000),
        (13.20, 13.55, 13.18, 13.50, 180_000),
        (13.50, 13.85, 13.48, 13.80, 200_000),
        (13.80, 13.82, 13.62, 13.66, 70_000),
        (13.66, 13.70, 13.55, 13.60, 55_000),
        (13.60, 13.66, 13.52, 13.58, 45_000),
        (13.58, 13.95, 13.56, 13.92, 220_000),
        (13.92, 14.05, 13.88, 14.00, 190_000),
    ]
    return [
        {"t": iso(start_utc + timedelta(minutes=i)), "o": o, "h": h, "l": l,
         "c": c, "v": v}
        for i, (o, h, l, c, v) in enumerate(specs)
    ]


class FakeAlpaca:
    """Market data + trading endpoints used across the pipeline."""

    def __init__(self):
        # 14:00 UTC = 10:00 ET regular hours
        self.minute = {"GOOD": bull_flag_payload(SESSION.replace(hour=14))}
        self.daily = {
            "GOOD": [
                {"t": iso(SESSION - timedelta(days=2)), "o": 9.8, "h": 10.2,
                 "l": 9.6, "c": 9.9, "v": 480_000},
                {"t": iso(SESSION - timedelta(days=1)), "o": 9.9, "h": 10.3,
                 "l": 9.8, "c": 10.0, "v": 520_000},
            ]
        }
        self.submitted = []
        self.fills = []

    # market data
    def get_minute_bars(self, symbols, start_iso, end_iso=None, feed=None, limit=10_000):
        return {s: self.minute.get(s, []) for s in symbols}

    def get_daily_bars(self, symbols, start_iso, end_iso=None):
        return {s: self.daily.get(s, []) for s in symbols}

    # trading
    def submit_order(self, **kw):
        self.submitted.append(kw)
        return {"id": f"broker-{len(self.submitted)}", "status": "accepted"}

    def get_account(self):
        return {"account_number": "PA-E2E", "equity": "100000",
                "cash": "50000", "buying_power": "200000"}

    def get_positions(self):
        return [
            {"symbol": o["symbol"], "qty": str(o["qty"]),
             "avg_entry_price": "13.9", "current_price": "14.1",
             "unrealized_pl": "2.0"}
            for o in self.submitted if o["side"] == "buy"
        ]

    def get_orders(self, status="all", limit=100):
        return [{"id": f"broker-{i+1}", "symbol": o["symbol"],
                 "status": "filled", "side": o["side"], "qty": str(o["qty"])}
                for i, o in enumerate(self.submitted)]


@pytest.fixture
def pipeline():
    client = FakeAlpaca()
    research_con = get_connection(":memory:")
    store = EventStore(":memory:")
    watcher = Watcher(
        store,
        ResearchWatchlistProvider(research_con, price_min=1, price_max=20),
        WatcherConfig(session_id="e2e", mode=EventMode.PAPER, min_quality=0.0),
    )
    executor = AlpacaPaperExecutor(store, client=client, session_id="e2e")
    execution = TradingExecutionService(
        store, executor=executor,
        settings=ExecutionSettings(auto_approve=False, max_orders_per_tick=2),
        trading_mode=TradingModeSettings(execution_mode="alpaca_paper"),
        session_id="e2e",
    )
    sync = AlpacaPaperSync(store, client=client, session_id="e2e")
    yield client, research_con, store, watcher, execution, sync
    store.close()
    research_con.close()


def test_full_paper_trading_pipeline(pipeline):
    client, research_con, store, watcher, execution, sync = pipeline

    # 1. ingest: daily baseline + live minute bars
    daily = ingest_daily_history(research_con, client, ["GOOD"])
    assert daily.daily_rows == 2
    minute = ingest_live_minute_bars(research_con, client, ["GOOD"])
    assert minute.minute_rows == 12

    # 2. watcher tick discovers + signals ready
    tick = watcher.tick(SESSION_DATE)
    assert tick.ready == ["GOOD"]
    signals = query_ready_signals_snapshot(store, session_id="e2e")
    assert signals[0]["symbol"] == "GOOD"
    assert signals[0]["entry_price"] > signals[0]["stop_loss_price"] > 0
    states = {w["symbol"]: w["state"] for w in query_watch_states_snapshot(store)}
    assert states["GOOD"] == "ready"

    # 3. broker sync populates account snapshots
    sync.sync_all()
    assert query_account_summary_snapshot(store, broker_name="alpaca_paper")

    # 4. execution tick: signal -> pending approval
    out = execution.tick()
    assert len(out["approvals_requested"]) == 1
    queue = query_approval_queue(store)
    assert queue[0]["symbol"] == "GOOD"
    assert queue[0]["approval_mode"] == "manual"

    # 5. manual approval submits the bracket order to the broker
    result = execution.approve_order(queue[0]["order_id"], approved_by="test")
    assert result["ok"]
    assert client.submitted[0]["symbol"] == "GOOD"
    assert client.submitted[0]["side"] == "buy"

    # 6. projections converge: order submitted, queue drained
    assert query_approval_queue(store) == []
    lifecycle = {o["order_id"]: o for o in query_order_lifecycle_snapshot(store)}
    entry = lifecycle[queue[0]["order_id"]]
    assert entry["symbol"] == "GOOD"
    assert entry["side"] == "buy"

    # 7. re-sync shows the open position; exit closes it
    sync.sync_all()
    exit_result = execution.submit_exit_order("GOOD")
    assert exit_result["ok"]
    assert client.submitted[-1]["side"] == "sell"

    # 8. watcher debounce: second tick emits no duplicate signal
    watcher.tick(SESSION_DATE)
    assert len(store.query_events(event_type="signal_ready", symbol="GOOD")) == 1
