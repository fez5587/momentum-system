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

from dotenv import load_dotenv

# Load .env so DATABASE_URL resolves to the configured Postgres (e.g.
# 192.168.1.5:5432) — the event store reads it. Without this the standalone
# dashboard fell back to the legacy local DuckDB path and showed nothing.
load_dotenv()

from api.main import DashboardState, create_server  # noqa: E402


def main() -> None:
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.environ.get("DASHBOARD_PORT", "8765"))
    # The datastore is Postgres (DATABASE_URL); the event store reads it directly.
    # WATCHER_EVENT_DB_PATH is a legacy fallback only (the path is ignored).
    db_path = os.environ.get("DATABASE_URL") or os.environ.get(
        "WATCHER_EVENT_DB_PATH", "momentum")
    mode = os.environ.get("TRADING_EXECUTION_MODE", "alpaca_paper")
    state = DashboardState(db_path, execution_service=None, execution_mode=mode)
    server = create_server(state, host, port)
    db_disp = db_path.split("@")[-1] if "@" in db_path else db_path
    print(f"[dashboard] serving http://{host}:{port} (event db: {db_disp}, mode: {mode})")
    print("[dashboard] read-only mode — run run_live_paper.py for live actions")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] stopped")


if __name__ == "__main__":
    main()
