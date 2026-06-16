"""Live paper-trading orchestrator (Milestone 4 end-to-end).

This is the piece that was missing: a single loop tying together

  1. ingest    — latest 1-minute Alpaca IEX bars into research market.duckdb
                 (plus a daily-bar backfill on startup so gap %% / RVOL work)
  2. watch     — Watcher.tick over the ingested bars, emitting signal_ready
  3. sync      — Alpaca paper account / positions / orders into the event DB
  4. execute   — TradingExecutionService.tick: approval requests, and order
                 submission (auto or via dashboard approval)
  5. dashboard — optional embedded API + UI at http://127.0.0.1:8010

Usage
-----
    python run_live_paper.py                 # continuous loop
    python run_live_paper.py --once          # single pass, then exit
    python run_live_paper.py --symbols AAPL,SNDL --auto-approve
    python run_live_paper.py --no-dashboard

Without ALPACA_API_KEY / ALPACA_SECRET_KEY the loop runs in dry mode:
network steps are skipped with clear messages, but watcher + dashboard
still work against whatever data already exists.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv

from alpaca_paper.client import AlpacaPaperClient
from alpaca_paper.execution import AlpacaPaperExecutor
from alpaca_paper.settings import AlpacaPaperSettings
from alpaca_paper.sync import AlpacaPaperSync
from api.main import DashboardState, serve_in_background
from research.ingestion.market_data import (
    classify_session,
    discover_active_symbols,
    ingest_daily_history,
    ingest_live_minute_bars,
)
from research.ingestion.scheduler import Scheduler
from research.ingestion.watcher_task import ResearchWatchlistProvider
from research.multi_schema import open_research_db
from runtime.watcher import Watcher, WatcherConfig
from storage.event_schema import EventMode
from storage.event_store import EventStore
from trading_execution import ExecutionSettings, TradingExecutionService
from trading_mode import TradingModeSettings

DEFAULT_SYMBOLS = ["AAPL", "TSLA", "AMD", "NVDA", "SNDL"]


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _now_session_date():
    session_date, _, _, _ = classify_session(datetime.now(timezone.utc))
    return session_date


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Momentum live paper-trading loop")
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    parser.add_argument(
        "--symbols",
        default=os.environ.get("WATCHER_SYMBOLS", ""),
        help="comma-separated symbols to watch (default: WATCHER_SYMBOLS env or built-ins)",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="auto-approve and submit ready signals (otherwise approve from the dashboard)",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="also pull symbols from Alpaca's most-actives screener",
    )
    parser.add_argument("--no-dashboard", action="store_true", help="skip the embedded dashboard")
    return parser.parse_args(argv)


def build_runtime(args: argparse.Namespace) -> dict:
    """Wire up every component; safe without API keys."""
    load_dotenv()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        symbols = DEFAULT_SYMBOLS

    alpaca_settings = AlpacaPaperSettings.from_env()
    has_keys = alpaca_settings.is_configured
    client = AlpacaPaperClient(alpaca_settings) if has_keys else None

    event_db = os.environ.get("WATCHER_EVENT_DB_PATH", "./data/momentum.duckdb")
    os.makedirs(os.path.dirname(event_db) or ".", exist_ok=True)
    store = EventStore(event_db)
    research_con = open_research_db("market")

    session_id = f"paper-{_now_session_date().isoformat()}-{uuid.uuid4().hex[:6]}"

    watcher = Watcher(
        store,
        ResearchWatchlistProvider(
            research_con,
            limit=int(os.environ.get("WATCHER_MAX_SYMBOLS", "25")),
        ),
        WatcherConfig(
            session_id=session_id,
            mode=EventMode.PAPER,
            max_symbols=int(os.environ.get("WATCHER_MAX_SYMBOLS", "25")),
            min_bars=int(os.environ.get("WATCHER_MIN_BARS", "10")),
            min_quality=float(os.environ.get("WATCHER_MIN_QUALITY", "0.30")),
        ),
    )

    exec_settings = ExecutionSettings.from_env()
    if args.auto_approve:
        exec_settings.auto_approve = True
    executor = AlpacaPaperExecutor(store, client=client, session_id=session_id) if client else None

    # Live last-trade price source for entry invalidation — tick-by-tick, not
    # bar close. Pulls the most recent trade from Alpaca's data API on demand;
    # a tiny TTL cache collapses redundant lookups within a single guard pass.
    # On any failure it returns None so the price-break check is simply skipped
    # for that pass (the wall-clock timeout still protects the entry) rather
    # than silently falling back to a stale bar close.
    _price_cache: dict[str, tuple[float, float]] = {}
    _PRICE_TTL = 2.0

    def latest_price(symbol: str):
        if client is None:
            return None
        import time as _time

        cached = _price_cache.get(symbol)
        now = _time.monotonic()
        if cached and (now - cached[1]) < _PRICE_TTL:
            return cached[0]
        try:
            trades = client.get_latest_trades([symbol])
            trade = trades.get(symbol) or trades.get(symbol.upper())
            price = float(trade["p"]) if trade and trade.get("p") is not None else None
        except Exception:
            price = None
        if price is not None:
            _price_cache[symbol] = (price, now)
        return price

    execution = (
        TradingExecutionService(
            store,
            executor=executor,
            settings=exec_settings,
            trading_mode=TradingModeSettings.from_env(),
            session_id=session_id,
            price_provider=latest_price,
        )
        if executor
        else None
    )
    sync = AlpacaPaperSync(store, client=client, session_id=session_id) if client else None

    return {
        "symbols": symbols,
        "client": client,
        "has_keys": has_keys,
        "store": store,
        "research_con": research_con,
        "watcher": watcher,
        "execution": execution,
        "sync": sync,
        "session_id": session_id,
        "event_db": event_db,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rt = build_runtime(args)
    symbols: list[str] = rt["symbols"]
    client = rt["client"]

    print(f"[boot] session={rt['session_id']} event_db={rt['event_db']}")
    print(f"[boot] watching: {', '.join(symbols)}")
    if not rt["has_keys"]:
        print("[boot] no ALPACA_API_KEY/SECRET — dry mode: ingestion, sync and "
              "order submission are skipped; watcher runs on existing data")

    # dashboard
    server = None
    if not args.no_dashboard and _flag("DASHBOARD_ENABLED", "1"):
        host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
        port = int(os.environ.get("DASHBOARD_PORT", "8010"))

        def inject_symbol(symbol: str) -> None:
            # add to the watcher's provider so it's evaluated next tick, and
            # to the live symbol list so the next ingest pass pulls its bars
            provider = rt["watcher"].provider
            if hasattr(provider, "add_symbol"):
                provider.add_symbol(symbol)
            if symbol not in symbols:
                symbols.append(symbol)

        state = DashboardState(
            rt["event_db"],
            execution_service=rt["execution"],
            execution_mode=TradingModeSettings.from_env().execution_mode,
            research_con=rt["research_con"],
            watch_inject=inject_symbol,
        )
        try:
            server, _ = serve_in_background(state, host, port)
            print(f"[boot] dashboard at http://{host}:{port}")
        except OSError as exc:
            print(f"[boot] dashboard disabled ({exc})")

    # startup: daily history backfill so gap%/RVOL have a baseline
    if client:
        if args.discover:
            discovered = discover_active_symbols(
                client,
                top=10,
                price_min=float(os.environ.get("WATCHER_PRICE_MIN", "1")),
                price_max=float(os.environ.get("WATCHER_PRICE_MAX", "20")),
            )
            if discovered:
                print(f"[boot] screener added: {', '.join(discovered)}")
                symbols = sorted(set(symbols) | set(discovered))
            else:
                print("[boot] screener unavailable — using static symbol list")
        daily = ingest_daily_history(rt["research_con"], client, symbols, days=30)
        print(f"[boot] daily backfill: {daily.daily_rows} rows"
              + (f" (errors: {daily.errors})" if daily.errors else ""))

    lookback = int(os.environ.get("LIVE_BARS_LOOKBACK_MINUTES", "240"))

    def step_ingest():
        if not client:
            return "skipped (no keys)"
        res = ingest_live_minute_bars(rt["research_con"], client, symbols, lookback_minutes=lookback)
        return f"{res.minute_rows} minute rows" + (f", errors={res.errors}" if res.errors else "")

    def step_watch():
        res = rt["watcher"].tick(_now_session_date())
        return (f"evaluated={res.evaluated} ready={res.ready} blocked={len(res.blocked)}"
                + (f" errors={res.errors}" if res.errors else ""))

    def step_sync():
        if not rt["sync"]:
            return "skipped (no keys)"
        rt["sync"].sync_all()
        return "ok"

    def step_execute():
        if not rt["execution"]:
            return "skipped (no keys)"
        res = rt["execution"].tick()
        backed = len(res.get("backed_out") or [])
        line = (f"approvals_requested={res['approvals_requested']} "
                f"auto_executed={len(res['auto_executed'])}")
        if backed:
            syms = ", ".join(b["symbol"] for b in res["backed_out"])
            line += f" backed_out={backed} ({syms})"
        return line

    def step_guard():
        # Fast invalidation watch: re-check armed (unfilled) entries against the
        # live last-trade price and the wall-clock timeout. Runs far more often
        # than `execute` so a break of the entry trigger is caught tick-by-tick
        # instead of waiting for the next execute pass.
        if not rt["execution"]:
            return None
        backed = rt["execution"].expire_stale_entries()
        if not backed:
            return None
        return "backed_out " + ", ".join(
            f"{b['symbol']} ({b['reason']})" for b in backed
        )

    scheduler = Scheduler()
    scheduler.add("ingest", step_ingest, float(os.environ.get("LIVE_BARS_INTERVAL_SECONDS", "60")),
                  enabled=_flag("LIVE_BARS_ENABLED", "1"))
    scheduler.add("watch", step_watch, float(os.environ.get("WATCHER_INTERVAL_SECONDS", "30")),
                  enabled=_flag("WATCHER_ENABLED", "1"))
    scheduler.add("sync", step_sync, float(os.environ.get("ALPACA_PAPER_SYNC_INTERVAL_SECONDS", "60")),
                  enabled=_flag("ALPACA_PAPER_SYNC_ENABLED", "1"))
    scheduler.add("execute", step_execute, float(os.environ.get("TRADING_EXECUTION_INTERVAL_SECONDS", "30")),
                  enabled=_flag("TRADING_EXECUTION_ENABLED", "1"))
    scheduler.add("guard", step_guard, float(os.environ.get("TRADING_ENTRY_GUARD_INTERVAL_SECONDS", "5")),
                  enabled=_flag("TRADING_EXECUTION_ENABLED", "1"))

    def run_pass():
        # force every task due, in pipeline order
        for task in scheduler.tasks:
            task.last_run = -10**9
        results = {}
        for name in ("ingest", "watch", "sync", "execute"):
            for task in scheduler.tasks:
                if task.name == name and task.enabled:
                    results.update(scheduler_run_one(task))
        stamp = datetime.now().strftime("%H:%M:%S")
        line = " | ".join(f"{k}: {v}" for k, v in results.items())
        print(f"[{stamp}] {line}")

    def scheduler_run_one(task):
        task.last_run = time.monotonic()
        task.run_count += 1
        try:
            out = task.func()
            task.last_error = None
            return {task.name: out}
        except Exception as exc:  # noqa: BLE001
            task.error_count += 1
            task.last_error = str(exc)
            return {task.name: f"ERROR {exc}"}

    if args.once:
        run_pass()
        if server:
            server.shutdown()
        return 0

    print("[boot] entering live loop (Ctrl-C to stop)")
    try:
        # one immediate full pass, then interval-driven
        run_pass()
        while True:
            results = scheduler.run_pending()
            if results:
                stamp = datetime.now().strftime("%H:%M:%S")
                line = " | ".join(
                    f"{k}: {v if not isinstance(v, Exception) else f'ERROR {v}'}"
                    for k, v in results.items()
                )
                print(f"[{stamp}] {line}")
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[stop] shutting down")
    finally:
        if server:
            server.shutdown()
        rt["store"].close()
        rt["research_con"].close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
