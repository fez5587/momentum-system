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
import json
import os
import sys
import threading
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
from research.ingestion.discovery import run_discovery, screen_universe
from research.multi_schema import open_research_db
from runtime.exit_manager import LiveExitManager
from runtime.triggers import ArmedTriggerBook
from runtime.watcher import Watcher, WatcherConfig
from strategy.exits import ExitConfig
from storage.event_schema import EventMode, ModuleTickEvent
from storage.event_store import EventStore
from trading_execution import ExecutionSettings, TradingExecutionService
from trading_mode import TradingModeSettings

# Fallback watchlist used ONLY if the sub-$20 screener is unavailable. Liquid
# small-caps that usually sit in the $1-20 band — NOT mega-caps (which the
# watcher's price band would just filter out, leaving nothing to trade).
DEFAULT_SYMBOLS = [
    "SOFI", "GRAB", "NU", "AAL", "PLUG", "RIOT",
    "MARA", "SNDL", "ACHR", "NOK", "SOUN", "BBAI",
]


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _now_session_date():
    session_date, _, _, _ = classify_session(datetime.now(timezone.utc))
    return session_date


def _load_learned() -> dict:
    """Nightly-tuned params from data/learned_params.json (empty if absent)."""
    path = os.path.join(os.environ.get("DATA_DIR", "./data"), "learned_params.json")
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def _db_target_desc() -> str:
    """Human-readable Postgres target for the boot log (password masked)."""
    url = os.environ.get("DATABASE_URL")
    if url:
        if "://" in url and "@" in url:
            scheme, rest = url.split("://", 1)
            creds, host = rest.split("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:***@{host}"
        return url
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    db = os.environ.get("PGDATABASE", "momentum")
    user = os.environ.get("PGUSER", "")
    return f"postgresql://{user}@{host}:{port}/{db}"


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

    explicit_symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    alpaca_settings = AlpacaPaperSettings.from_env()
    has_keys = alpaca_settings.is_configured
    client = AlpacaPaperClient(alpaca_settings) if has_keys else None

    event_db = os.environ.get("WATCHER_EVENT_DB_PATH", "./data/momentum.duckdb")
    os.makedirs(os.path.dirname(event_db) or ".", exist_ok=True)
    store = EventStore(event_db)
    research_con = open_research_db("market")

    session_id = f"paper-{_now_session_date().isoformat()}-{uuid.uuid4().hex[:6]}"

    # Watchlist resolution. Explicit --symbols/WATCHER_SYMBOLS win; otherwise
    # discover the sub-$20 most-actives universe via the screener (the strategy
    # targets $1-20 small-caps, not the hardcoded mega-cap fallback). Falls back
    # to DEFAULT_SYMBOLS only if the screener is unavailable.
    price_min = float(os.environ.get("WATCHER_PRICE_MIN", "1"))
    price_max = float(os.environ.get("WATCHER_PRICE_MAX", "20"))
    discover_top = int(os.environ.get("DISCOVER_TOP", "20"))
    if explicit_symbols:
        symbols = explicit_symbols
        symbols_source = "explicit"
    elif client is not None and _flag("DISCOVER_ON_START", "1"):
        discovered = screen_universe(
            client, price_min=price_min, price_max=price_max, top=discover_top
        )
        symbols = discovered or list(DEFAULT_SYMBOLS)
        symbols_source = "screener" if discovered else "fallback"
    else:
        symbols = list(DEFAULT_SYMBOLS)
        symbols_source = "fallback"

    store.emit(
        ModuleTickEvent(
            timestamp=datetime.now(),
            mode=EventMode.PAPER,
            correlation_id=session_id,
            message=f"watchlist resolved via {symbols_source}: {len(symbols)} symbols",
            module="discovery",
            stage="boot",
            duration_ms=0.0,
            input_count=0,
            output_count=len(symbols),
            metrics={"source": symbols_source, "symbols": symbols},
            errors=(
                []
                if symbols_source != "fallback"
                else [{"error": "screener returned 0 names at boot — using fallback "
                       "watchlist; check Alpaca screener / data plan"}]
            ),
        )
    )

    _learned = _load_learned()
    if _learned:
        print(f"[boot] learned params: ready_score={_learned.get('ready_score_pct')} "
              f"min_bars={_learned.get('min_bars')} setups={_learned.get('setups')} "
              f"(tuned {str(_learned.get('as_of', ''))[:10]} over {_learned.get('sessions')} "
              f"sessions; backtest pnl {_learned.get('pnl')}, entry@{_learned.get('entry_min')}min)")

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
            # Defaults come from the nightly self-tuner (learned_params.json);
            # an explicit env var still wins.
            min_bars=int(os.environ.get("WATCHER_MIN_BARS", str(_learned.get("min_bars", 10)))),
            ready_score_pct=float(os.environ.get(
                "WATCHER_READY_SCORE", str(_learned.get("ready_score_pct", 60.0)))),
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

    # Fast armed-trigger book: keep the N most-promising gappers ready with a
    # pre-computed opening-range trigger/stop so step_trigger can fire the
    # instant live price crosses (instead of waiting for a bar close + cadence).
    book = ArmedTriggerBook(
        max_armed=int(os.environ.get("TRIGGER_MAX_ARMED", "6")),
        gap_min=float(os.environ.get("TRIGGER_GAP_MIN", "3.0")),
        gap_max=float(os.environ.get("TRIGGER_GAP_MAX", "1e9")),
        rvol_min=float(os.environ.get("TRIGGER_RVOL_MIN", "2.0")),
        min_range_pct=float(os.environ.get("TRIGGER_MIN_RANGE_PCT", "0.004")),
        min_dollar_vol=float(os.environ.get("TRIGGER_MIN_DOLLAR_VOL", "0")),
        max_price_age_s=float(os.environ.get("TRIGGER_PRICE_MAX_AGE", "12")),
    )

    return {
        "symbols": symbols,
        "symbols_source": symbols_source,
        "client": client,
        "has_keys": has_keys,
        "store": store,
        "research_con": research_con,
        "watcher": watcher,
        "execution": execution,
        "sync": sync,
        "book": book,
        "latest_price": latest_price,
        "session_id": session_id,
        "event_db": event_db,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rt = build_runtime(args)
    symbols: list[str] = rt["symbols"]
    client = rt["client"]

    print(f"[boot] session={rt['session_id']} db={_db_target_desc()}")
    print(f"[boot] watching ({rt['symbols_source']}): {', '.join(symbols)}")
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
    refresh_lb = int(os.environ.get("LIVE_BARS_REFRESH_MINUTES", "20"))
    _ingest_first = {"v": True}
    # freshness watchdog: warn when the newest bar is older than this during RTH.
    _ingest_stale_secs = float(os.environ.get("INGEST_STALE_SECONDS", "180"))
    # block arming on stale data? OFF by default — observability first; a
    # mis-tuned threshold must not silently halt trading.
    _ingest_stale_block = os.environ.get("INGEST_STALE_BLOCK", "0").strip() in {"1", "true", "yes"}
    _data_stale = {"v": False}

    def _first_pass_lookback() -> int:
        # The first ingest must cover the whole session-so-far so the
        # opening-range bars land in the DB (even on a mid-session restart);
        # after that we only pull the last few minutes — closed bars never
        # change, and re-fetching a 4-hour window for 80 symbols every minute
        # was the ~46s step that froze the loop.
        try:
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
            open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            since_open = max(0.0, (now_et - open_et).total_seconds() / 60.0)
        except Exception:  # noqa: BLE001
            since_open = 0.0
        return int(max(lookback, since_open + 15))

    def _freshest_bar_age():
        """Seconds since the newest stored minute bar (UTC), or None. The bar
        timestamp is its START, and IEX lags ~1 bar, so ~90-120s is NORMAL during
        RTH — only flag well beyond that."""
        from datetime import timezone
        try:
            row = rt["research_con"].execute(
                "SELECT max(timestamp) FROM minute_bars WHERE session_date = ?",
                [_now_session_date()]).fetchone()
            if not row or not row[0]:
                return None
            ts = row[0]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - ts).total_seconds()
        except Exception:  # noqa: BLE001
            return None

    def _is_rth():
        from zoneinfo import ZoneInfo
        n = datetime.now(ZoneInfo("America/New_York"))
        return n.weekday() < 5 and (9, 30) <= (n.hour, n.minute) < (16, 0)

    def step_ingest():
        if not client:
            return "skipped (no keys)"
        if _ingest_first["v"]:
            lb = _first_pass_lookback()
            _ingest_first["v"] = False
        else:
            lb = refresh_lb
        res = ingest_live_minute_bars(rt["research_con"], client, symbols, lookback_minutes=lb)
        # Freshness watchdog: 45% of passes historically returned 0 rows with NO
        # error — indistinguishable from a feed outage. Make staleness VISIBLE
        # (emit + display); hard-block arming only if explicitly enabled, so a
        # mis-tuned threshold can't silently halt trading.
        age = _freshest_bar_age()
        stale = bool(_is_rth() and age is not None and age > _ingest_stale_secs)
        _data_stale["v"] = stale
        if stale:
            rt["store"].emit(ModuleTickEvent(
                timestamp=datetime.now(), mode=EventMode.PAPER,
                correlation_id=rt["session_id"],
                message=f"INGEST STALE: freshest bar {age:.0f}s old (> {_ingest_stale_secs:.0f}s)",
                module="ingestion", stage="stale", duration_ms=0.0,
                input_count=len(symbols), output_count=res.minute_rows,
                metrics={"bar_age_s": age}, errors=[{"error": "stale_data"}]))
        rt["store"].emit(
            ModuleTickEvent(
                timestamp=datetime.now(),
                mode=EventMode.PAPER,
                correlation_id=rt["session_id"],
                message=f"ingested {res.minute_rows} minute rows across {len(res.symbols)} symbols",
                module="ingestion",
                stage="completed",
                duration_ms=0.0,
                input_count=len(symbols),
                output_count=res.minute_rows,
                metrics={"per_symbol": res.per_symbol, "symbols_with_data": res.symbols},
                errors=[{"error": e} for e in res.errors],
            )
        )
        note = (f" STALE({age:.0f}s)" if stale else "")
        return (f"{res.minute_rows} rows (lookback {lb}m){note}"
                + (f", errors={res.errors}" if res.errors else ""))

    def step_watch():
        res = rt["watcher"].tick(_now_session_date())
        return (f"evaluated={res.evaluated} ready={res.ready} blocked={len(res.blocked)}"
                + (f" errors={res.errors}" if res.errors else ""))

    def step_sync():
        if not rt["sync"]:
            return "skipped (no keys)"
        res = rt["sync"].sync_all()
        failed = [k for k, v in res.items() if v is None]
        return "ok" if not failed else f"DEGRADED ({', '.join(failed)})"

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

    def step_discover():
        if not client:
            return "skipped (no keys)"
        res = run_discovery(
            rt["store"], rt["research_con"], client, _now_session_date(),
            mode=EventMode.PAPER, correlation_id=rt["session_id"],
            price_min=float(os.environ.get("WATCHER_PRICE_MIN", "1")),
            price_max=float(os.environ.get("WATCHER_PRICE_MAX", "20")),
            top=int(os.environ.get("DISCOVER_TOP", "20")),
        )
        # fold newly-screened names into the live watchlist so the next ingest
        # pulls their bars and the watcher evaluates them
        added = [s for s in res.universe if s not in symbols]
        symbols.extend(added)
        # Keep the watchlist BOUNDED so it rotates toward current movers instead
        # of growing all session. (Ingestion is now batched so an over-cap list
        # no longer 400s, but an unbounded list is still wasteful and stale.)
        max_uni = int(os.environ.get("WATCHER_MAX_UNIVERSE", "80"))
        if len(symbols) > max_uni:
            del symbols[: len(symbols) - max_uni]
        return (f"universe={len(res.universe)} gappers={len(res.gappers)}"
                + (f" +{len(added)} new" if added else ""))

    # --- fast armed-trigger path -------------------------------------------
    book = rt["book"]
    price_min_v = float(os.environ.get("WATCHER_PRICE_MIN", "1"))
    price_max_v = float(os.environ.get("WATCHER_PRICE_MAX", "20"))
    orb_bars_v = int(os.environ.get("ORB_BARS", "5"))
    stop_cushion_v = float(os.environ.get("TRIGGER_STOP_CUSHION", "0.005"))

    def step_arm():
        """Rank the most-promising gappers and pre-compute each one's opening-
        range breakout trigger/stop, so step_trigger can fire on a live cross."""
        from research import query as rq
        from research.ingestion.discovery import recent_news_map
        from research.ingestion.signals import scan_gappers
        from strategy.evaluation.structure import opening_range

        # don't arm new triggers on stale data when blocking is enabled — a break
        # computed off a frozen feed could fire into a name that has already moved.
        if _ingest_stale_block and _data_stale["v"]:
            return "skipped: data stale (INGEST_STALE_BLOCK)"
        session_date = _now_session_date()
        # fresh catalysts (the discover step ingests the feeds; we just read them)
        news_map = recent_news_map(
            rt["research_con"], lookback_hours=int(os.environ.get("NEWS_LOOKBACK_HOURS", "8")))
        news_boost = float(os.environ.get("NEWS_RANK_BOOST", "2.0"))
        # optional MANUAL per-symbol priority nudge (SYMBOL_WEIGHTS=ABCD:1.2,XYZ:1.1)
        # — a small boost to names you want watched; keep modest so it doesn't
        # override the gap/volume logic.
        weights: dict[str, float] = {}
        for part in os.environ.get("SYMBOL_WEIGHTS", "").split(","):
            if ":" in part:
                s, w = part.split(":", 1)
                try:
                    weights[s.strip().upper()] = float(w)
                except ValueError:
                    pass
        candidates: list[dict] = []
        # rank ALL session symbols by gap*rvol (thresholds applied later, per
        # trigger, so the board still shows the field pre-gap)
        gappers = scan_gappers(
            rt["research_con"], session_date,
            min_gap_pct=0.0, min_relative_volume=0.0,
            price_min=price_min_v, price_max=price_max_v,
            limit=max(12, book.max_armed + 6),
        )
        for g in gappers:
            bars = rq.query_minute_bars(rt["research_con"], g.symbol, session_date)
            hi, lo, complete = opening_range(bars, orb_bars=orb_bars_v)
            stop = lo * (1.0 - stop_cushion_v) if lo else None
            catalyst = news_map.get(g.symbol, "")
            # recent $-volume (last 5 regular-hours bars) — the liquidity gate.
            # rvol is relative; this is the absolute "can it absorb an order" check.
            dollar_vol = 0.0
            try:
                rth = bars[bars["is_regular_hours"]] if "is_regular_hours" in bars else bars
                tail = rth.tail(5)
                dollar_vol = float((tail["close"] * tail["volume"]).sum())
            except Exception:  # noqa: BLE001
                dollar_vol = 0.0
            candidates.append({
                "symbol": g.symbol,
                "gap_pct": g.gap_pct,
                "rvol": g.relative_volume,
                "trigger": hi,
                "stop": stop,
                "range_pct": ((hi - lo) / hi) if (hi and lo and hi > 0) else 0.0,
                "cum_volume": g.cumulative_volume,
                "dollar_vol": dollar_vol,
                "catalyst": catalyst,
                # a fresh catalyst is WHY a small-cap runs — boost it up the rank;
                # times an optional manual per-symbol weight (default 1.0)
                "_score": (g.gap_pct * max(g.relative_volume, 0.1)
                           * (news_boost if catalyst else 1.0)
                           * weights.get(g.symbol, 1.0)),
                "complete": complete,
            })
        # pre-open / before any gappers form: surface the queued watchlist so the
        # board shows what we're about to watch instead of looking blank
        if not candidates:
            for s in symbols[: book.max_armed]:
                candidates.append({"symbol": s, "gap_pct": 0.0, "rvol": 0.0,
                                   "trigger": None, "stop": None,
                                   "range_pct": 0.0, "complete": False})
        # catalyst-backed gappers float to the top of the armed set
        candidates.sort(key=lambda c: c.get("_score", 0.0), reverse=True)
        book.arm(candidates)
        armed = sum(1 for t in book.triggers.values() if t.state == "armed")
        n_news = sum(1 for c in candidates if c.get("catalyst"))
        return f"tracking={len(book.triggers)} armed={armed}" + (f" news={n_news}" if n_news else "")

    # The trigger runs on its OWN thread, not the scheduler, so a slow ingest
    # can never freeze breakout detection. It only touches the (thread-safe)
    # book, execution service, and event store — never research_con.
    trigger_interval = float(os.environ.get("TRIGGER_INTERVAL_SECONDS", "4"))
    stop_event = threading.Event()

    def _batch_prices(syms):
        """One batched last-trade call for all armed names (8 syms ≈ 1 sym cost)."""
        if client is None or not syms:
            return {}
        try:
            trades = client.get_latest_trades(list(syms))
        except Exception:  # noqa: BLE001
            return {}
        out = {}
        for s in syms:
            tr = trades.get(s) or trades.get(s.upper())
            if tr and tr.get("p") is not None:
                try:
                    out[s] = float(tr["p"])
                except (TypeError, ValueError):
                    pass
        return out

    def _trigger_pass():
        snap = book.snapshot()
        syms = [r["symbol"] for r in snap]
        if not syms:
            return
        for s, px in _batch_prices(syms).items():
            book.update_price(s, px)
        fired: list[str] = []
        if rt["execution"] is not None:
            try:
                book.mark_filled(rt["execution"]._held_symbols())
            except Exception:  # noqa: BLE001
                pass
            for t in book.fires():
                res = rt["execution"].submit_breakout_now(
                    t.symbol, t.trigger, t.stop, last_price=t.price,
                    cum_volume=t.cum_volume,
                )
                if res.get("ok"):
                    book.mark_fired(t.symbol)
                    fired.append(t.symbol)
        snap = book.snapshot()
        armed_n = sum(1 for r in snap if r["state"] == "armed")
        rt["store"].emit(
            ModuleTickEvent(
                timestamp=datetime.now(), mode=EventMode.PAPER,
                correlation_id=rt["session_id"],
                message=f"triggers armed={armed_n} fired={len(fired)}",
                module="triggers", stage="snapshot", duration_ms=0.0,
                input_count=len(syms), output_count=len(fired),
                metrics={"triggers": snap, "armed": armed_n}, errors=[],
            )
        )
        if fired:
            print(f"[{datetime.now():%H:%M:%S}] TRIGGER FIRED {', '.join(fired)}", flush=True)

    def trigger_loop():
        while not stop_event.wait(trigger_interval):
            try:
                _trigger_pass()
            except Exception as exc:  # noqa: BLE001
                print(f"[trigger] error: {exc}", flush=True)

    # --- live exit management (breakeven / trail / scale / first-red) -------
    exit_cfg = ExitConfig.from_env()

    def _rth_bars(sym):
        from research import query as rq
        b = rq.query_minute_bars(rt["research_con"], sym, _now_session_date())
        if b is not None and len(b) and "is_regular_hours" in b.columns:
            b = b[b["is_regular_hours"] == True].reset_index(drop=True)  # noqa: E712
        return b

    exit_mgr = (
        LiveExitManager(client, rt["store"], _rth_bars, cfg=exit_cfg,
                        session_id=rt["session_id"])
        if client else None
    )
    print(f"[boot] exit management: {exit_cfg.describe()}")

    def step_manage_exits():
        if exit_mgr is None:
            return None
        acts = exit_mgr.manage()
        return ", ".join(acts) if acts else None

    # --- end-of-day auto-flatten (a day-trading book shouldn't gap overnight) --
    eod_enabled = _flag("TRADING_EOD_FLATTEN_ENABLED", "1")
    try:
        eod_h, eod_m = (int(x) for x in
                        os.environ.get("TRADING_EOD_FLATTEN_TIME", "15:55").split(":"))
    except Exception:  # noqa: BLE001
        eod_h, eod_m = 15, 55
    _eod_done = {"v": False}

    def step_eod_flatten():
        if not eod_enabled or _eod_done["v"] or rt["execution"] is None:
            return None
        try:
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
        except Exception:  # noqa: BLE001
            return None
        cur = (now_et.hour, now_et.minute)
        # only inside [flatten_time, 16:00): after the close a market order just
        # queues uselessly, so don't fire then (e.g. on a post-close restart)
        if now_et.weekday() >= 5 or not ((eod_h, eod_m) <= cur < (16, 0)):
            return None
        res = rt["execution"].close_session("eod_flatten")
        # only mark done if it actually succeeded — otherwise RETRY on the next
        # pass within the window (a flatten that errored once left positions naked
        # overnight). close_session re-blocks entries each call, so retrying is safe.
        _eod_done["v"] = not res["errors"]
        closed = ", ".join(res["closed_positions"]) or "none"
        return (f"EOD FLATTEN closed [{closed}]"
                + (f" errors={res['errors']} (will retry)" if res["errors"] else ""))

    scheduler = Scheduler()
    scheduler.add("discover", step_discover,
                  float(os.environ.get("DISCOVER_INTERVAL_SECONDS", "300")),
                  enabled=_flag("DISCOVER_ENABLED", "1"))
    scheduler.add("ingest", step_ingest, float(os.environ.get("LIVE_BARS_INTERVAL_SECONDS", "60")),
                  enabled=_flag("LIVE_BARS_ENABLED", "1"))
    scheduler.add("watch", step_watch, float(os.environ.get("WATCHER_INTERVAL_SECONDS", "30")),
                  enabled=_flag("WATCHER_ENABLED", "1"))
    scheduler.add("arm", step_arm, float(os.environ.get("TRIGGER_ARM_INTERVAL_SECONDS", "20")),
                  enabled=_flag("TRIGGER_ENABLED", "1"))
    scheduler.add("sync", step_sync, float(os.environ.get("ALPACA_PAPER_SYNC_INTERVAL_SECONDS", "60")),
                  enabled=_flag("ALPACA_PAPER_SYNC_ENABLED", "1"))
    scheduler.add("execute", step_execute, float(os.environ.get("TRADING_EXECUTION_INTERVAL_SECONDS", "30")),
                  enabled=_flag("TRADING_EXECUTION_ENABLED", "1"))
    scheduler.add("guard", step_guard, float(os.environ.get("TRADING_ENTRY_GUARD_INTERVAL_SECONDS", "5")),
                  enabled=_flag("TRADING_EXECUTION_ENABLED", "1"))
    scheduler.add("exits", step_manage_exits, float(os.environ.get("TRADING_EXIT_MANAGE_INTERVAL_SECONDS", "12")),
                  enabled=_flag("TRADING_EXECUTION_ENABLED", "1"))
    scheduler.add("eod", step_eod_flatten, float(os.environ.get("TRADING_EOD_INTERVAL_SECONDS", "30")),
                  enabled=_flag("TRADING_EXECUTION_ENABLED", "1"))

    def run_pass():
        # force every task due, in pipeline order
        for task in scheduler.tasks:
            task.last_run = -10**9
        results = {}
        for name in ("discover", "ingest", "watch", "arm", "sync", "execute", "exits", "eod"):
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
    # start the fast trigger on its own thread so slow ingest can't freeze it
    trigger_thread = None
    if _flag("TRIGGER_ENABLED", "1") and client is not None:
        trigger_thread = threading.Thread(target=trigger_loop, name="trigger", daemon=True)
        trigger_thread.start()
        print(f"[boot] fast trigger thread started (every {trigger_interval:.0f}s)")
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
        stop_event.set()
        if trigger_thread is not None:
            trigger_thread.join(timeout=5)
        if server:
            server.shutdown()
        rt["store"].close()
        rt["research_con"].close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
