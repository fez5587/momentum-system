"""Dashboard API (Milestone 5) — stdlib ThreadingHTTPServer, no FastAPI.

Endpoints
---------
GET  /                                  dashboard HTML
GET  /api/snapshots                     everything the dashboard needs (one shot)
GET  /api/stream                        Server-Sent Events: pushes snapshots on change
GET  /api/approval-queue                pending approvals only
GET  /api/criteria?symbol=XYZ           per-criterion pass/fail breakdown
GET  /api/bars?symbol=XYZ[&minutes=60]  recent 1-min bars for a sparkline
POST /api/trading/approvals/approve     {"order_id": ..., "approved_by"?: ...}
POST /api/trading/approvals/reject      {"order_id": ..., "reason"?: ...}
POST /api/trading/exit-order            {"symbol": ...}
POST /api/watch/add                     {"symbol": ...}  (manual watchlist inject)

The server holds an EventStore for reads; trade actions route through a
TradingExecutionService when one is attached (the orchestrator does this).
Without one, action endpoints return 503 instead of crashing, so the
dashboard still works read-only.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from storage.event_store import EventStore
from storage.projections import (
    query_account_orders_snapshot,
    query_account_positions_snapshot,
    query_account_summary_snapshot,
    query_approval_queue,
    query_fills_feed,
    query_order_lifecycle_snapshot,
    query_ready_signals_snapshot,
    query_risk_state,
    query_session_pnl,
    query_session_summary,
    query_symbol_criteria,
    query_watch_states_snapshot,
)

STATIC_DIR = Path(__file__).parent / "static"


def _eastern_session_date():
    """Today's date in US/Eastern — matches the ingestion pipeline's flags."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/New_York")).date()


class DashboardState:
    """Shared state: event store path + optional live execution service."""

    def __init__(
        self,
        event_db_path: str,
        execution_service=None,
        execution_mode: str = "alpaca_paper",
        research_con=None,
        watch_inject=None,
    ) -> None:
        self.event_db_path = event_db_path
        self.execution_service = execution_service
        self.execution_mode = execution_mode
        # optional research DB connection + callback for charts / manual inject
        self.research_con = research_con
        self.watch_inject = watch_inject
        self._lock = threading.Lock()

    def open_store(self) -> EventStore:
        return EventStore(self.event_db_path)

    def snapshots(self) -> dict:
        with self._lock:
            store = self.open_store()
            try:
                return {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "execution_mode": self.execution_mode,
                    "has_execution": self.execution_service is not None,
                    "pnl": query_session_pnl(store),
                    "risk": query_risk_state(store),
                    "approval_queue": query_approval_queue(store),
                    "ready_signals": query_ready_signals_snapshot(store),
                    "watch_states": query_watch_states_snapshot(store),
                    "accounts": query_account_summary_snapshot(store),
                    "positions": query_account_positions_snapshot(store),
                    "orders": query_account_orders_snapshot(store),
                    "order_lifecycle": query_order_lifecycle_snapshot(store),
                    "fills": query_fills_feed(store, limit=30),
                    "sessions": query_session_summary(store),
                }
            finally:
                store.close()

    def event_count(self) -> int:
        with self._lock:
            store = self.open_store()
            try:
                return store.count_events()
            finally:
                store.close()

    def approval_queue(self) -> list[dict]:
        with self._lock:
            store = self.open_store()
            try:
                return query_approval_queue(store)
            finally:
                store.close()

    def criteria(self, symbol: str) -> dict:
        with self._lock:
            store = self.open_store()
            try:
                return query_symbol_criteria(store, symbol)
            finally:
                store.close()

    def bars(self, symbol: str, minutes: int = 60) -> dict:
        """Recent 1-min bars for a sparkline, from the research DB if present.

        Uses the current US/Eastern session date (matching the ingestion
        pipeline). If today has no bars yet (pre-market, weekend, or replay of
        an older session), falls back to the most recent session that does.
        """
        if self.research_con is None:
            return {"symbol": symbol, "bars": [], "reason": "no research db attached"}
        from research.query import query_minute_bars

        with self._lock:
            try:
                session_date = _eastern_session_date()
                df = query_minute_bars(self.research_con, symbol, session_date)
                if df is None or df.empty:
                    # fall back to the latest session with bars for this symbol
                    row = self.research_con.execute(
                        "SELECT MAX(session_date) FROM minute_bars WHERE symbol = ?",
                        [symbol],
                    ).fetchone()
                    if row and row[0] is not None:
                        df = query_minute_bars(self.research_con, symbol, row[0])
            except Exception as exc:  # noqa: BLE001
                return {"symbol": symbol, "bars": [], "reason": str(exc)}
        if df is None or df.empty:
            return {"symbol": symbol, "bars": []}
        tail = df.tail(minutes)
        bars = [
            {
                "t": str(row["timestamp"]),
                "c": float(row["close"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "v": int(row["volume"]),
                "vwap": float(row["vwap"]) if row.get("vwap") is not None else None,
            }
            for _, row in tail.iterrows()
        ]
        return {"symbol": symbol, "bars": bars}

    def _with_service(self, fn):
        if self.execution_service is None:
            return None
        with self._lock:
            return fn(self.execution_service)

    def approve(self, order_id: str, approved_by: str, notes: str | None) -> dict | None:
        return self._with_service(
            lambda svc: svc.approve_order(order_id, approved_by=approved_by, notes=notes)
        )

    def reject(self, order_id: str, rejected_by: str, reason: str) -> dict | None:
        return self._with_service(
            lambda svc: svc.reject_order(order_id, rejected_by=rejected_by, reason=reason)
        )

    def exit_position(self, symbol: str) -> dict | None:
        return self._with_service(lambda svc: svc.submit_exit_order(symbol))

    def add_watch(self, symbol: str) -> dict | None:
        """Manually inject a symbol onto the watchlist (if supported)."""
        if not self.watch_inject or not symbol:
            return None
        try:
            self.watch_inject(symbol.upper())
            return {"ok": True, "symbol": symbol.upper()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def _dump(payload) -> bytes:
    return json.dumps(payload, default=_json_default).encode()


class DashboardHandler(BaseHTTPRequestHandler):
    state: DashboardState  # injected via make_handler
    protocol_version = "HTTP/1.1"

    # -- plumbing -----------------------------------------------------------

    def log_message(self, fmt, *args):  # noqa: A003 - quiet by default
        pass

    def _send_json(self, payload, status: int = 200) -> None:
        body = _dump(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path) -> None:
        if not path.exists():
            self._send_json({"error": "dashboard.html not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode() or "{}")
        except json.JSONDecodeError:
            return {}

    def _query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    # -- SSE ----------------------------------------------------------------

    def _stream(self) -> None:
        """Server-Sent Events: push a fresh snapshot whenever events change.

        Falls back gracefully — a heartbeat every ~15s keeps proxies open even
        when nothing is happening, and the loop exits when the client drops or
        the server signals shutdown (so the DuckDB handle is released cleanly).
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        shutdown = getattr(type(self), "shutdown_event", None)
        last_count = -1
        last_heartbeat = 0.0
        try:
            while shutdown is None or not shutdown.is_set():
                count = self.state.event_count()
                now = time.monotonic()
                if count != last_count:
                    last_count = count
                    payload = _dump(self.state.snapshots()).decode()
                    self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                    last_heartbeat = now
                elif now - last_heartbeat > 15:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_heartbeat = now
                # sleep in short slices so shutdown is responsive
                for _ in range(10):
                    if shutdown is not None and shutdown.is_set():
                        break
                    time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # client disconnected

    # -- routes ---------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        try:
            route = urlparse(self.path).path
            if route in ("/", "/index.html", "/dashboard"):
                self._send_html(STATIC_DIR / "dashboard.html")
            elif route == "/api/stream":
                self._stream()
            elif route == "/api/snapshots":
                self._send_json(self.state.snapshots())
            elif route == "/api/approval-queue":
                self._send_json({"approval_queue": self.state.approval_queue()})
            elif route == "/api/criteria":
                symbol = (self._query().get("symbol") or [""])[0]
                self._send_json(self.state.criteria(symbol))
            elif route == "/api/bars":
                q = self._query()
                symbol = (q.get("symbol") or [""])[0]
                minutes = int((q.get("minutes") or ["60"])[0])
                self._send_json(self.state.bars(symbol, minutes))
            elif route == "/api/health":
                self._send_json({"ok": True, "mode": self.state.execution_mode})
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)

    def do_POST(self):  # noqa: N802
        try:
            route = urlparse(self.path).path
            body = self._read_body()
            if route == "/api/trading/approvals/approve":
                result = self.state.approve(
                    str(body.get("order_id") or ""),
                    approved_by=str(body.get("approved_by") or "dashboard"),
                    notes=body.get("notes"),
                )
            elif route == "/api/trading/approvals/reject":
                result = self.state.reject(
                    str(body.get("order_id") or ""),
                    rejected_by=str(body.get("rejected_by") or "dashboard"),
                    reason=str(body.get("reason") or "manual"),
                )
            elif route == "/api/trading/exit-order":
                result = self.state.exit_position(str(body.get("symbol") or ""))
            elif route == "/api/watch/add":
                result = self.state.add_watch(str(body.get("symbol") or ""))
            else:
                self._send_json({"error": "not found"}, 404)
                return
            if result is None:
                self._send_json(
                    {"ok": False, "error": "not available in read-only mode"}, 503
                )
            else:
                self._send_json(result, 200 if result.get("ok") else 400)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def make_handler(state: DashboardState, shutdown_event=None):
    return type(
        "BoundDashboardHandler",
        (DashboardHandler,),
        {"state": state, "shutdown_event": shutdown_event},
    )


def create_server(
    state: DashboardState, host: str = "127.0.0.1", port: int = 8010
) -> ThreadingHTTPServer:
    shutdown_event = threading.Event()
    server = ThreadingHTTPServer((host, port), make_handler(state, shutdown_event))
    # let SSE loops notice shutdown and release their DuckDB handles cleanly
    server._sse_shutdown = shutdown_event  # type: ignore[attr-defined]
    _orig_shutdown = server.shutdown

    def _shutdown():
        shutdown_event.set()
        _orig_shutdown()

    server.shutdown = _shutdown  # type: ignore[method-assign]
    return server


def serve_in_background(
    state: DashboardState, host: str = "127.0.0.1", port: int = 8010
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = create_server(state, host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
