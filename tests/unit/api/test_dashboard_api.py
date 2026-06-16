"""Dashboard API endpoint tests over a real local HTTP server."""

import json
import urllib.request
from datetime import datetime

import pytest

from api.main import DashboardState, serve_in_background
from storage.event_schema import EventMode, SignalReadyEvent
from storage.event_store import EventStore
from tests.unit.test_trading_execution import FakeExecutor, make_service

T0 = datetime(2026, 6, 11, 9, 45)


@pytest.fixture
def server(tmp_path):
    db_path = str(tmp_path / "events.duckdb")
    # seed a ready signal
    store = EventStore(db_path)
    store.emit(
        SignalReadyEvent(
            timestamp=T0, mode=EventMode.PAPER, correlation_id="t",
            message="GOOD ready", symbol="GOOD", signal_type="bull_flag",
            confidence=0.9,
            signal_data={"entry_price": 14.0, "stop_loss_price": 13.45,
                         "quality_score": 0.7},
        )
    )
    service = make_service(store, FakeExecutor())
    service.request_approvals_for_ready_signals()

    state = DashboardState(db_path, execution_service=service,
                           execution_mode="alpaca_paper")
    httpd, _ = serve_in_background(state, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, store
    httpd.shutdown()
    store.close()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def post(base, path, body):
    request = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_snapshots_endpoint_shape(server):
    base, _ = server
    status, data = get(base, "/api/snapshots")
    assert status == 200
    for key in ("execution_mode", "approval_queue", "ready_signals",
                "watch_states", "accounts", "positions", "orders",
                "order_lifecycle"):
        assert key in data
    assert data["execution_mode"] == "alpaca_paper"
    assert data["ready_signals"][0]["symbol"] == "GOOD"
    assert len(data["approval_queue"]) == 1


def test_approval_queue_endpoint(server):
    base, _ = server
    status, data = get(base, "/api/approval-queue")
    assert status == 200
    assert data["approval_queue"][0]["symbol"] == "GOOD"


def test_approve_endpoint_executes_and_clears_queue(server):
    base, _ = server
    _, data = get(base, "/api/approval-queue")
    order_id = data["approval_queue"][0]["order_id"]
    status, result = post(base, "/api/trading/approvals/approve",
                          {"order_id": order_id})
    assert status == 200 and result["ok"]
    _, after = get(base, "/api/approval-queue")
    assert after["approval_queue"] == []


def test_reject_endpoint(server):
    base, _ = server
    _, data = get(base, "/api/approval-queue")
    order_id = data["approval_queue"][0]["order_id"]
    status, result = post(base, "/api/trading/approvals/reject",
                          {"order_id": order_id, "reason": "test"})
    assert status == 200 and result["ok"]


def test_approve_unknown_order_returns_400(server):
    base, _ = server
    status, result = post(base, "/api/trading/approvals/approve",
                          {"order_id": "nope"})
    assert status == 400
    assert not result["ok"]


def test_unknown_route_404(server):
    base, _ = server
    try:
        urllib.request.urlopen(base + "/api/nope", timeout=5)
        raised = False
    except urllib.error.HTTPError as exc:
        raised = exc.code == 404
    assert raised


def test_dashboard_html_served(server):
    base, _ = server
    with urllib.request.urlopen(base + "/", timeout=5) as resp:
        assert resp.status == 200
        body = resp.read().decode()
    assert "Momentum" in body and "/api/snapshots" in body


def test_actions_503_without_execution_service(tmp_path):
    state = DashboardState(str(tmp_path / "e.duckdb"), execution_service=None)
    httpd, _ = serve_in_background(state, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, result = post(base, "/api/trading/exit-order", {"symbol": "GOOD"})
        assert status == 503
        assert not result["ok"]
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# Interactive endpoints (SSE, criteria, bars, manual inject) + new snapshot keys
# ---------------------------------------------------------------------------

def test_snapshot_includes_pnl_and_fills(server):
    base, _ = server
    _, data = get(base, "/api/snapshots")
    assert "pnl" in data and "fills" in data and "has_execution" in data
    assert data["has_execution"] is True
    # pnl has the expected stat keys even with no closed trades yet
    for key in ("realized_pnl", "unrealized_pnl", "total_pnl", "win_rate",
                "open_positions", "closed_trades"):
        assert key in data["pnl"]


def test_criteria_endpoint(tmp_path):
    from storage.event_schema import CriteriaEvaluatedEvent
    db_path = str(tmp_path / "crit.duckdb")
    store = EventStore(db_path)
    store.emit(
        CriteriaEvaluatedEvent(
            timestamp=T0, mode=EventMode.PAPER, message="c", symbol="GOOD",
            criteria_results={"passed": ["gap", "vwap"], "failed": ["impulse"]},
            total_criteria=9, passed_criteria=2, success_score_pct=22.0,
        )
    )
    state = DashboardState(db_path, execution_mode="alpaca_paper")
    httpd, _ = serve_in_background(state, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, data = get(base, "/api/criteria?symbol=GOOD")
        assert status == 200
        assert data["symbol"] == "GOOD"
        assert data["score"] == 22.0
        by_key = {c["key"]: c["passed"] for c in data["criteria"]}
        assert by_key["gap"] is True
        assert by_key["impulse"] is False
    finally:
        httpd.shutdown()
        store.close()


def test_bars_endpoint_with_research_db(tmp_path):
    from research.ingestion.market_data import (
        ingest_daily_history,
        ingest_live_minute_bars,
    )
    from storage.db import get_connection
    from tests.integration.test_end_to_end import FakeAlpaca

    con = get_connection(":memory:")
    client = FakeAlpaca()
    ingest_daily_history(con, client, ["GOOD"])
    ingest_live_minute_bars(con, client, ["GOOD"])

    db_path = str(tmp_path / "bars.duckdb")
    EventStore(db_path).close()
    state = DashboardState(db_path, execution_mode="alpaca_paper", research_con=con)
    httpd, _ = serve_in_background(state, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, data = get(base, "/api/bars?symbol=GOOD&minutes=40")
        assert status == 200
        # falls back to the latest available session for the symbol
        assert len(data["bars"]) == 12
        assert all("c" in b for b in data["bars"])
    finally:
        httpd.shutdown()


def test_bars_endpoint_no_research_db(server):
    base, _ = server
    status, data = get(base, "/api/bars?symbol=GOOD")
    assert status == 200
    assert data["bars"] == []


def test_watch_add_endpoint(tmp_path):
    db_path = str(tmp_path / "watch.duckdb")
    EventStore(db_path).close()
    injected = []
    state = DashboardState(
        db_path, execution_mode="alpaca_paper",
        watch_inject=lambda sym: injected.append(sym),
    )
    httpd, _ = serve_in_background(state, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, result = post(base, "/api/watch/add", {"symbol": "tsla"})
        assert status == 200 and result["ok"]
        assert result["symbol"] == "TSLA"
        assert injected == ["TSLA"]
    finally:
        httpd.shutdown()


def test_watch_add_503_without_inject(server):
    base, _ = server  # this server has no watch_inject configured
    status, result = post(base, "/api/watch/add", {"symbol": "TSLA"})
    assert status == 503
    assert not result["ok"]


def test_stream_delivers_snapshot_frame(server):
    base, _ = server
    # read the SSE stream until we see one snapshot event, then bail
    req = urllib.request.urlopen(base + "/api/stream", timeout=5)
    try:
        buf = b""
        got = False
        for _ in range(50):
            line = req.readline()
            if not line:
                break
            buf += line
            if b"event: snapshot" in buf and buf.rstrip().endswith(b"}"):
                got = True
                break
        assert got, "no snapshot frame received from SSE stream"
    finally:
        req.close()
