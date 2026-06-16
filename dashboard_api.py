"""Run the dashboard API + UI standalone (read-only against the event DB).

    python dashboard_api.py
    # open http://127.0.0.1:8010

Env: DASHBOARD_HOST, DASHBOARD_PORT, WATCHER_EVENT_DB_PATH,
TRADING_EXECUTION_MODE. For approve/reject/exit buttons to act, run the full
orchestrator (run_live_paper.py) instead, which attaches the execution
service to this same server.
"""

from __future__ import annotations

import os

from api.main import DashboardState, create_server


def main() -> None:
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8010"))
    db_path = os.environ.get("WATCHER_EVENT_DB_PATH", "./data/momentum.duckdb")
    mode = os.environ.get("TRADING_EXECUTION_MODE", "alpaca_paper")
    state = DashboardState(db_path, execution_service=None, execution_mode=mode)
    server = create_server(state, host, port)
    print(f"[dashboard] serving http://{host}:{port} (event db: {db_path}, mode: {mode})")
    print("[dashboard] read-only mode — run run_live_paper.py for live actions")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] stopped")


if __name__ == "__main__":
    main()
