#!/usr/bin/env python3
"""momentum — inspect & verify each pipeline stage against Postgres.

Per-section verification on demand, so you can confirm with your own eyes that
each stage is doing what it should:

    python momentum_cli.py doctor                 # PASS/WARN/FAIL across stages
    python momentum_cli.py inspect events          # recent event stream
    python momentum_cli.py inspect bars            # per-symbol bar counts + freshness
    python momentum_cli.py inspect discovery       # latest screened universe + gappers
    python momentum_cli.py inspect criteria SYMBOL # per-criterion pass/fail

Reads the same Postgres the app writes (DATABASE_URL / PG* from .env). Doctor
exits non-zero if any check FAILs, so it is scriptable / CI-able.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import box

load_dotenv()

app = typer.Typer(add_completion=False, help=__doc__)
inspect_app = typer.Typer(help="Inspect a single pipeline stage.")
app.add_typer(inspect_app, name="inspect")
console = Console()

EXPECTED_TABLES = 23


def _con():
    from storage.db_pg import get_connection
    return get_connection()


def _session_date():
    from research.ingestion.market_data import classify_session
    d, _, _, _ = classify_session(datetime.now(timezone.utc))
    return d


def _events_of(con, module=None, stage=None, event_type="module_tick", limit=1):
    sql = "SELECT timestamp, message, payload_json FROM events WHERE event_type = ?"
    params = [event_type]
    if module:
        sql += " AND payload_json LIKE ?"
        params.append(f'%"module": "{module}"%')
    if stage:
        sql += " AND payload_json LIKE ?"
        params.append(f'%"stage": "{stage}"%')
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    return con.execute(sql, params).fetchall()


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def _row(results, name, status, detail):
    results.append((name, status, detail))


@app.command()
def doctor():
    """Run PASS / WARN / FAIL health checks across every pipeline stage."""
    results: list[tuple[str, str, str]] = []
    con = None
    try:
        con = _con()
        _row(results, "database", "PASS", "connected via DATABASE_URL/PG env")
    except Exception as e:
        _row(results, "database", "FAIL", f"cannot connect: {e}")
        _render_doctor(results)
        raise typer.Exit(1)

    # schema
    try:
        n = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
        ).fetchone()[0]
        status = "PASS" if n >= EXPECTED_TABLES else "FAIL"
        _row(results, "schema", status, f"{n} tables (expected >= {EXPECTED_TABLES})")
    except Exception as e:
        _row(results, "schema", "FAIL", str(e))

    sess = _session_date()

    # daily bars + freshness
    try:
        cnt, syms, latest = con.execute(
            "SELECT count(*), count(distinct symbol), max(trade_date) FROM daily_bars"
        ).fetchone()
        if cnt == 0:
            _row(results, "research.daily_bars", "FAIL", "no daily bars (gap%/RVOL have no baseline)")
        else:
            stale_days = (sess - latest).days if latest else 999
            status = "PASS" if stale_days <= 4 else "WARN"
            _row(results, "research.daily_bars", status,
                 f"{cnt} rows, {syms} symbols, latest {latest} ({stale_days}d old)")
    except Exception as e:
        _row(results, "research.daily_bars", "FAIL", str(e))

    # minute bars today (distinguishes market-closed from broken)
    try:
        cnt, syms, latest = con.execute(
            "SELECT count(*), count(distinct symbol), max(timestamp) FROM minute_bars WHERE session_date = ?",
            [sess],
        ).fetchone()
        if cnt == 0:
            _row(results, "ingestion.minute_bars", "WARN",
                 f"0 minute bars for {sess} — market closed / thin feed (not necessarily broken)")
        else:
            _row(results, "ingestion.minute_bars", "PASS",
                 f"{cnt} bars across {syms} symbols today, newest {latest}")
    except Exception as e:
        _row(results, "ingestion.minute_bars", "FAIL", str(e))

    # bars-collected telemetry present
    try:
        ev = _events_of(con, module="ingestion", stage="completed")
        if ev:
            _row(results, "ingestion.telemetry", "PASS", f"last bars-collected event {ev[0][0]}")
        else:
            _row(results, "ingestion.telemetry", "WARN", "no bars-collected events yet (run the app)")
    except Exception as e:
        _row(results, "ingestion.telemetry", "FAIL", str(e))

    # discovery
    try:
        ev = _events_of(con, module="discovery", stage="completed")
        if ev:
            payload = json.loads(ev[0][2])
            m = payload.get("metrics", {})
            n_uni, n_gap = len(m.get("universe", [])), len(m.get("gappers", []))
            errs = payload.get("errors", [])
            if n_uni == 0:
                _row(results, "discovery", "FAIL",
                     f"screener returned 0 names @ {ev[0][0]} — check Alpaca screener/limits"
                     + (f"; {errs}" if errs else ""))
            else:
                _row(results, "discovery", "PASS" if not errs else "WARN",
                     f"{n_uni} sub-$20 names, {n_gap} gappers @ {ev[0][0]}"
                     + (f"; errors={errs}" if errs else ""))
        else:
            _row(results, "discovery", "WARN", "no discovery events yet (run the app)")
    except Exception as e:
        _row(results, "discovery", "FAIL", str(e))

    # event stream
    try:
        n = con.execute("SELECT count(*) FROM events").fetchone()[0]
        _row(results, "event_store", "PASS" if n else "WARN", f"{n} events")
    except Exception as e:
        _row(results, "event_store", "FAIL", str(e))

    # honest known-gap warnings (audit findings — surfaced, not hidden)
    _row(results, "known-gap: circuit breaker", "WARN",
         "daily-loss circuit breaker is parsed but NOT implemented")
    _row(results, "known-gap: quality_score", "WARN",
         "quality_score is hardcoded 1.0 (not a real data-quality metric)")
    _row(results, "known-gap: ETF universe", "WARN",
         "discovery universe can include leveraged ETFs (SOXS/TZA/TSLL); no ETF filter yet")

    _render_doctor(results)
    if any(s == "FAIL" for _, s, _ in results):
        raise typer.Exit(1)


def _render_doctor(results):
    table = Table(title="momentum doctor", box=box.ROUNDED, show_lines=False)
    table.add_column("stage", style="bold")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    color = {"PASS": "[green]PASS[/green]", "WARN": "[yellow]WARN[/yellow]", "FAIL": "[red]FAIL[/red]"}
    for name, status, detail in results:
        table.add_row(name, color.get(status, status), detail)
    console.print(table)
    fails = sum(s == "FAIL" for _, s, _ in results)
    warns = sum(s == "WARN" for _, s, _ in results)
    console.print(f"[bold]{'OK' if not fails else 'PROBLEMS'}[/bold] — "
                  f"[green]{sum(s=='PASS' for _,s,_ in results)} pass[/green], "
                  f"[yellow]{warns} warn[/yellow], [red]{fails} fail[/red]")


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------

@inspect_app.command("events")
def inspect_events(limit: int = 20, type: str = typer.Option(None, help="filter by event_type")):
    """Recent events from the event store."""
    con = _con()
    sql = "SELECT timestamp, event_type, message FROM events"
    params = []
    if type:
        sql += " WHERE event_type = ?"
        params.append(type)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    rows = con.execute(sql, params).fetchall()
    table = Table(title=f"events (latest {limit})", box=box.SIMPLE)
    table.add_column("timestamp", style="cyan")
    table.add_column("type", style="magenta")
    table.add_column("message", overflow="fold")
    for ts, et, msg in rows:
        table.add_row(str(ts), et, msg or "")
    console.print(table)
    if not rows:
        console.print("[yellow]no events — run the app first[/yellow]")


@inspect_app.command("bars")
def inspect_bars(date: str = typer.Option(None, help="session date YYYY-MM-DD (default: today ET)")):
    """Per-symbol bar counts + freshness (the 'are bars collected?' answer)."""
    con = _con()
    sess = date or _session_date().isoformat()
    rows = con.execute(
        """
        SELECT COALESCE(m.symbol, d.symbol) AS symbol,
               COALESCE(m.bars, 0) AS minute_bars, m.newest,
               COALESCE(d.dbars, 0) AS daily_bars, d.latest_daily
        FROM (SELECT symbol, count(*) bars, max(timestamp) newest
              FROM minute_bars WHERE session_date = ? GROUP BY symbol) m
        FULL OUTER JOIN (SELECT symbol, count(*) dbars, max(trade_date) latest_daily
              FROM daily_bars GROUP BY symbol) d ON d.symbol = m.symbol
        ORDER BY minute_bars DESC, symbol ASC
        """,
        [sess],
    ).fetchall()
    table = Table(title=f"bars for session {sess}", box=box.SIMPLE)
    table.add_column("symbol", style="bold")
    table.add_column("minute bars", justify="right")
    table.add_column("newest minute", style="cyan")
    table.add_column("daily bars", justify="right")
    table.add_column("latest daily", style="cyan")
    total_min = 0
    for sym, mb, newest, db, latest in rows:
        total_min += mb or 0
        flag = "" if mb else "  [yellow](0 — closed/thin)[/yellow]"
        table.add_row(sym, str(mb), str(newest or "-") + flag, str(db), str(latest or "-"))
    console.print(table)
    console.print(f"[bold]{len(rows)} symbols[/bold], {total_min} minute bars total for {sess}")


@inspect_app.command("discovery")
def inspect_discovery():
    """Latest screened sub-$20 universe + ranked gappers."""
    con = _con()
    ev = _events_of(con, module="discovery", stage="completed")
    if not ev:
        console.print("[yellow]no discovery events yet — run the app[/yellow]")
        return
    ts, msg, payload = ev[0]
    m = json.loads(payload).get("metrics", {})
    console.print(f"[bold]{msg}[/bold]  [dim]{ts}[/dim]")
    band = m.get("price_band", [1, 20])
    console.print(f"universe (${band[0]:g}-${band[1]:g}): {', '.join(m.get('universe', [])) or '(none)'}")
    gappers = m.get("gappers", [])
    if gappers:
        table = Table(title="ranked gappers", box=box.SIMPLE)
        for c in ("rank", "symbol", "price", "gap_pct", "rvol"):
            table.add_column(c, justify="right" if c != "symbol" else "left")
        for g in gappers:
            table.add_row(str(g.get("rank")), g.get("symbol"), f"{g.get('price')}",
                          f"{g.get('gap_pct')}%", f"{g.get('rvol')}x")
        console.print(table)
    else:
        console.print("[yellow]0 gappers[/yellow] — needs intraday minute bars (market hours)")


@inspect_app.command("criteria")
def inspect_criteria(symbol: str):
    """Per-criterion pass/fail for a symbol (from the latest evaluation)."""
    con = _con()
    rows = con.execute(
        """
        SELECT timestamp, payload_json FROM events
        WHERE event_type = 'criteria_evaluated' AND payload_json LIKE ?
        ORDER BY timestamp DESC LIMIT 1
        """,
        [f'%"symbol": "{symbol.upper()}"%'],
    ).fetchall()
    if not rows:
        console.print(f"[yellow]no criteria evaluation for {symbol.upper()} yet[/yellow] "
                      "(needs minute bars + a watcher pass during market hours)")
        return
    ts, payload = rows[0]
    p = json.loads(payload)
    cr = p.get("criteria_results", {})
    status = cr.get("status", "?")
    sc = {"ready": "green", "blocked": "red", "late": "yellow"}.get(status, "white")
    gap = cr.get("gap_pct")
    rvol = cr.get("relative_volume")
    gap_s = f"{gap:+.1%}" if isinstance(gap, (int, float)) else str(gap)
    rvol_s = f"{rvol:.1f}x" if isinstance(rvol, (int, float)) else str(rvol)
    console.print(
        f"[bold]{symbol.upper()}[/bold]  [{sc}]{status.upper()}[/{sc}]  "
        f"score={p.get('success_score_pct')}%  passed={p.get('passed_criteria')}/{p.get('total_criteria')}  "
        f"gap={gap_s}  rvol={rvol_s}  [dim]{ts}[/dim]"
    )
    if cr.get("reason"):
        console.print(f"[dim]reason:[/dim] {cr['reason']}")
    table = Table(box=box.SIMPLE)
    table.add_column("criterion", style="bold")
    table.add_column("result")
    table.add_column("reason", overflow="fold")
    detail = cr.get("detail", [])
    if detail:
        for d in detail:
            ok = d.get("passed")
            table.add_row(d.get("name", "?"),
                          "[green]pass[/green]" if ok else "[red]fail[/red]",
                          d.get("reason") or "")
    else:  # older events without detail
        for n in cr.get("passed", []):
            table.add_row(n, "[green]pass[/green]", "")
        for n in cr.get("failed", []):
            table.add_row(n, "[red]fail[/red]", "")
    console.print(table)


@inspect_app.command("signals")
def inspect_signals(limit: int = 25):
    """Ready signals + the latest per-symbol evaluation board (gap/rvol/status)."""
    con = _con()
    console.print("[bold]READY signals[/bold]")
    rows = con.execute(
        "SELECT timestamp, message FROM events WHERE event_type='signal_ready' "
        "ORDER BY timestamp DESC LIMIT ?", [limit]).fetchall()
    if rows:
        t = Table(box=box.SIMPLE)
        t.add_column("time", style="cyan"); t.add_column("signal")
        for ts, msg in rows:
            t.add_row(str(ts), msg or "")
        console.print(t)
    else:
        console.print("[yellow]no ready signals yet (none, or after the 10:30 ET cutoff)[/yellow]")

    console.print("\n[bold]latest evaluations[/bold] (most recent per symbol)")
    rows = con.execute(
        "SELECT payload_json FROM events WHERE event_type='criteria_evaluated' "
        "ORDER BY timestamp DESC LIMIT 400").fetchall()
    seen: dict = {}
    for (pj,) in rows:
        p = json.loads(pj)
        sym = p.get("symbol")
        if sym and sym not in seen:
            cr = p.get("criteria_results", {})
            seen[sym] = (p.get("success_score_pct") or 0, cr.get("status"),
                         cr.get("gap_pct"), cr.get("relative_volume"))
    if not seen:
        console.print("[yellow]no evaluations yet (needs minute bars + a watcher pass)[/yellow]")
        return
    t = Table(box=box.SIMPLE)
    for c in ("symbol", "status", "score", "gap", "rvol"):
        t.add_column(c, justify="left" if c == "symbol" else "right")
    for sym, (score, status, gap, rvol) in sorted(seen.items(), key=lambda x: -(x[1][0] or 0)):
        color = {"ready": "green", "late": "yellow", "blocked": "red"}.get(status, "white")
        t.add_row(sym, f"[{color}]{status}[/{color}]", f"{score}%",
                  f"{gap:+.1%}" if isinstance(gap, (int, float)) else str(gap),
                  f"{rvol:.1f}x" if isinstance(rvol, (int, float)) else str(rvol))
    console.print(t)


def _latest_eval_board(con) -> dict:
    """Most-recent evaluation per symbol: {sym: (score, status, gap, rvol, reason)}."""
    rows = con.execute(
        "SELECT payload_json FROM events WHERE event_type='criteria_evaluated' "
        "ORDER BY timestamp DESC LIMIT 800").fetchall()
    seen: dict = {}
    for (pj,) in rows:
        p = json.loads(pj)
        sym = p.get("symbol")
        if sym and sym not in seen:
            cr = p.get("criteria_results", {})
            seen[sym] = (p.get("success_score_pct") or 0, cr.get("status"),
                         cr.get("gap_pct"), cr.get("relative_volume"), cr.get("reason"))
    return seen


def _market_clock():
    """(phase, detail, color) for the US equities regular session, in ET."""
    from datetime import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        now = _dt.now(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001
        now = _dt.now()
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now.weekday() >= 5:
        return ("CLOSED", "weekend", "dim")
    if now < open_t:
        s = int((open_t - now).total_seconds())
        return ("PRE-OPEN", f"opens in {s // 3600}h {s % 3600 // 60}m", "yellow")
    if now > close_t:
        return ("CLOSED", "after hours", "dim")
    s = int((close_t - now).total_seconds())
    return ("OPEN", f"{s // 3600}h {s % 3600 // 60}m to close", "green")


def _triggers_snapshot(con):
    """Latest armed-trigger snapshot emitted by the live loop's `trigger` step.

    Returns (rows, snapshot_timestamp, armed_count). rows is the per-symbol
    board data (state/price/gap/rvol/trigger/distance/stop).
    """
    rows = con.execute(
        "SELECT timestamp, payload_json FROM events WHERE event_type='module_tick' "
        "ORDER BY timestamp DESC LIMIT 120").fetchall()
    for ts, pj in rows:
        p = json.loads(pj)
        if p.get("module") == "triggers":
            metrics = p.get("metrics") or {}
            return (metrics.get("triggers") or [], ts, metrics.get("armed", 0))
    return ([], None, 0)


def _heartbeat(ts) -> tuple[str, str]:
    """(text, color) describing how long ago the loop last published."""
    if ts is None:
        return ("no loop signal yet", "red")
    from datetime import datetime as _dt
    try:
        when = ts if isinstance(ts, _dt) else _dt.fromisoformat(str(ts))
        secs = (_dt.now() - when.replace(tzinfo=None)).total_seconds()
    except Exception:  # noqa: BLE001
        return ("live", "green")
    color = "green" if secs < 15 else ("yellow" if secs < 45 else "red")
    return (f"{int(secs)}s", color)


_STATE_STYLE = {
    "armed": ("bold cyan", "● ARMED"),
    "fired": ("bold magenta", "▶ FIRED"),
    "filled": ("bold green", "◆ LONG"),
    "waiting": ("dim", "· waiting"),
    "weak": ("dim yellow", "  weak"),
}


def _to_et(iso: str):
    """Parse an ISO-8601 (UTC) broker timestamp to America/New_York. The broker
    reports UTC; a US-market journal must read in ET. None if unparseable."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    try:
        dt = _dt.fromisoformat((iso or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001
        return None


def _todays_trades(con):
    """Reconstruct TODAY's round-trip trades (FIFO buy->sell per symbol) from the
    latest broker orders snapshot, plus still-open lots. Resets each day (filters
    to today's fills, in ET). Returns (trades, realized_pnl). Times are ET."""
    from collections import defaultdict, deque
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    today = _dt.now(ZoneInfo("America/New_York")).date().isoformat()
    rows = con.execute("SELECT payload_json FROM events WHERE event_type='account_orders_updated' "
                       "ORDER BY timestamp DESC LIMIT 1").fetchall()
    if not rows:
        return [], 0.0
    orders = json.loads(rows[0][0]).get("orders") or []
    fills = [o for o in orders
             if float(o.get("filled_quantity") or 0) > 0
             and (_to_et(o.get("submitted_at")) is not None)
             and _to_et(o.get("submitted_at")).date().isoformat() == today]
    fills.sort(key=lambda o: o.get("submitted_at") or "")
    lots: dict = defaultdict(deque)
    trades = []
    for o in fills:
        sym, side = o.get("symbol"), o.get("side")
        qty = float(o.get("filled_quantity") or 0)
        px = float(o.get("filled_avg_price") or 0)
        _et = _to_et(o.get("submitted_at"))
        tm = _et.strftime("%H:%M:%S") if _et else ""
        if side == "buy":
            lots[sym].append([qty, px, tm])
        elif side == "sell":
            rem = qty
            while rem > 1e-9 and lots[sym]:
                lot = lots[sym][0]
                m = min(rem, lot[0])
                trades.append({"symbol": sym, "qty": m, "entry": lot[1], "exit": px,
                               "pnl": (px - lot[1]) * m, "time": lot[2], "status": "closed"})
                lot[0] -= m
                rem -= m
                if lot[0] <= 1e-9:
                    lots[sym].popleft()
    for sym, dq in lots.items():
        for qty, px, tm in dq:
            trades.append({"symbol": sym, "qty": qty, "entry": px, "exit": None,
                           "pnl": None, "time": tm, "status": "open"})
    realized = sum(t["pnl"] for t in trades if t["status"] == "closed")
    return trades, realized


def _day_pnl(con):
    """Authoritative day P&L = latest equity - prior-session close (broker truth,
    resets daily). Uses the broker's last_equity baseline; falls back to the
    first equity recorded today for events emitted before that field existed."""
    latest = con.execute("SELECT payload_json FROM events WHERE event_type='account_summary_updated' "
                         "ORDER BY timestamp DESC LIMIT 1").fetchall()
    if not latest:
        return None
    p = json.loads(latest[0][0])
    try:
        e_now = float(p.get("total_equity") or 0)
        last_eq = float(p.get("last_equity") or 0)
        if e_now and last_eq:
            return e_now - last_eq
    except (TypeError, ValueError):
        pass
    first = con.execute("SELECT payload_json FROM events WHERE event_type='account_summary_updated' "
                        "AND timestamp::date = CURRENT_DATE ORDER BY timestamp ASC LIMIT 1").fetchall()
    if not first:
        return None
    try:
        e_now = float(p.get("total_equity") or 0)
        e_start = float(json.loads(first[0][0]).get("total_equity") or 0)
        return (e_now - e_start) if (e_now and e_start) else None
    except (TypeError, ValueError):
        return None


def _open_unrealized(con):
    """Sum of unrealized P&L across the latest open-positions snapshot (broker
    average-cost basis). Pairs with _day_pnl so realized = day_pnl - open."""
    pos_ev = con.execute(
        "SELECT payload_json FROM events WHERE event_type='account_positions_updated' "
        "ORDER BY timestamp DESC LIMIT 1").fetchall()
    total = 0.0
    if pos_ev:
        for p in (json.loads(pos_ev[0][0]).get("positions") or []):
            try:
                total += float(p.get("unrealized_pnl") if p.get("unrealized_pnl") is not None
                               else p.get("unrealized_pl") or 0)
            except (TypeError, ValueError):
                pass
    return total


_PROT_ACTIVE = {"held", "new", "accepted", "pending_new",
                "accepted_for_bidding", "partially_filled"}


def _unprotected_positions(con):
    """Long positions whose RESTING protective sell coverage is short of the held
    quantity — i.e. naked / under-protected. Computed from the latest synced
    positions + orders snapshots (the order sync now flattens bracket legs, so a
    held stop/TP leg is visible). This is the alarm we lacked when APWC/NOWL went
    naked and the board showed nothing. Returns [{symbol, qty, protected}]."""
    pos_ev = con.execute(
        "SELECT payload_json FROM events WHERE event_type='account_positions_updated' "
        "ORDER BY timestamp DESC LIMIT 1").fetchall()
    ord_ev = con.execute(
        "SELECT payload_json FROM events WHERE event_type='account_orders_updated' "
        "ORDER BY timestamp DESC LIMIT 1").fetchall()
    if not pos_ev:
        return []
    positions = json.loads(pos_ev[0][0]).get("positions") or []
    orders = json.loads(ord_ev[0][0]).get("orders") or [] if ord_ev else []
    sell_cover: dict = {}
    for o in orders:
        if (o.get("side") == "sell" and o.get("status") in _PROT_ACTIVE
                and o.get("type") in ("stop", "stop_limit", "limit", "trailing_stop")):
            sym = o.get("symbol")
            qty = float(o.get("quantity") or 0) - float(o.get("filled_quantity") or 0)
            sell_cover[sym] = sell_cover.get(sym, 0.0) + max(qty, 0.0)
    out = []
    for p in positions:
        sym = p.get("symbol")
        try:
            qty = float(p.get("quantity") if p.get("quantity") is not None
                        else p.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty > 0 and sell_cover.get(sym, 0.0) < qty - 1e-6:  # long, under-covered
            out.append({"symbol": sym, "qty": qty, "protected": sell_cover.get(sym, 0.0)})
    return out


def _matched_realized(day_pnl, open_unreal, fifo_realized):
    """Broker-authoritative realized for the header: day P&L minus current open
    unrealized, so the parenthetical reconciles exactly (matched + open == day
    P&L). Falls back to the FIFO sum when the broker day P&L is unavailable.
    (Per-trade rows stay FIFO-accurate; their sum can differ from this only by
    the average-cost-vs-FIFO split on partially-closed names.)"""
    if day_pnl is None:
        return fifo_realized
    return day_pnl - open_unreal


def _render_board(con):
    """Build the live monitoring board renderable."""
    from rich.console import Group
    from rich.panel import Panel

    acct = con.execute(
        "SELECT payload_json FROM events WHERE event_type='account_summary_updated' "
        "ORDER BY timestamp DESC LIMIT 1").fetchall()
    eq = "—"
    if acct:
        eq = json.loads(acct[0][0]).get("total_equity", "—")

    phase, detail, pcolor = _market_clock()
    trig_rows, snap_ts, armed_n = _triggers_snapshot(con)
    hb_text, hb_color = _heartbeat(snap_ts)

    # --- armed triggers: the one action surface. Show every actionable row
    # (armed/fired/long) uncapped, then at most the top few scanning rows by rvol,
    # dropping corrupt-data noise (0-volume / absurd-gap rows) that buries the signal.
    tt = Table(box=box.SIMPLE_HEAVY, expand=True,
               title="ARMED TRIGGERS  [dim](· armed  ▶ fired  ◆ long)[/dim]")
    for c, j in (("symbol", "left"), ("state", "left"), ("price", "right"),
                 ("gap", "right"), ("rvol", "right"), ("trigger", "right"),
                 ("→trigger", "right"), ("stop", "right")):
        tt.add_column(c, justify=j, no_wrap=True)
    tt.add_column("catalyst", justify="left", overflow="ellipsis", max_width=34)

    _ACTIONABLE = {"armed", "fired", "filled"}

    def _real_row(r):                       # drop 0-volume / corrupt-gap scanning noise
        rv, gp = r.get("rvol"), r.get("gap")
        if not rv:
            return False
        return not (isinstance(gp, (int, float)) and abs(gp) > 1000)

    actionable = [r for r in trig_rows if r.get("state") in _ACTIONABLE]
    scanning = sorted((r for r in trig_rows if r.get("state") not in _ACTIONABLE and _real_row(r)),
                      key=lambda r: (r.get("rvol") or 0), reverse=True)
    shown_rows = actionable + scanning[:4]
    hidden_scan = max(0, len(scanning) - 4)
    for r in shown_rows:
        style, label = _STATE_STYLE.get(r.get("state"), ("white", r.get("state", "?")))
        price = r.get("price"); trig = r.get("trigger"); dist = r.get("dist")
        stop = r.get("stop"); gap = r.get("gap"); rvol = r.get("rvol")
        cat = r.get("catalyst") or ""
        sym = r.get("symbol")
        tt.add_row(
            f"[{style}]{'📰' if cat else ''}{sym}[/{style}]",
            f"[{style}]{label}[/{style}]",
            f"{price:.2f}" if isinstance(price, (int, float)) else "—",
            f"{gap:+.1f}%" if isinstance(gap, (int, float)) else "—",
            f"{rvol:.1f}x" if isinstance(rvol, (int, float)) else "—",
            f"{trig:.2f}" if isinstance(trig, (int, float)) else "—",
            (f"[green]▲{dist * 100:+.2f}%[/green]" if isinstance(dist, (int, float)) and dist >= 0
             else (f"{dist * 100:+.2f}%" if isinstance(dist, (int, float)) else "—")),
            f"{stop:.2f}" if isinstance(stop, (int, float)) else "—",
            f"[cyan]{cat}[/cyan]" if cat else "[dim]—[/dim]",
        )
    if not shown_rows:
        tt.add_row("[dim]—[/dim]", "[dim]waiting for the open / first bars[/dim]",
                   "—", "—", "—", "—", "—", "—", "—")
    elif hidden_scan:
        tt.add_row(f"[dim]+{hidden_scan} more[/dim]", "[dim]scanning[/dim]",
                   "—", "—", "—", "—", "—", "—", "—")

    open_unreal = _open_unrealized(con)
    trades, realized = _todays_trades(con)
    day_pnl = _day_pnl(con)
    if day_pnl is None:            # fallback if equity snapshots missing
        day_pnl = realized + open_unreal
    matched = _matched_realized(day_pnl, open_unreal, realized)
    dcol = "green" if day_pnl >= 0 else "red"
    # day P&L lives once in the header; the title carries only the unique matched/open split
    pt = Table(title=f"today's trades  [dim](matched {matched:+,.0f} · open {open_unreal:+,.0f})[/dim]",
               box=box.SIMPLE)
    for c in ("time", "symbol", "qty", "entry", "exit", "P&L"):
        pt.add_column(c, justify="left" if c == "symbol" else "right")
    # open lots float to the top and are NEVER capped off; closed fill the rest up to 6
    _tr = sorted(trades, key=lambda x: x["time"], reverse=True)
    opens = [t for t in _tr if t["pnl"] is None]
    closed = [t for t in _tr if t["pnl"] is not None]
    budget = max(0, 6 - len(opens))
    for t in opens + closed[:budget]:
        pnl = t["pnl"]
        pnl_str = "[dim]—[/dim]" if pnl is None else f"[{'green' if pnl >= 0 else 'red'}]{pnl:+.0f}[/]"
        mark = "[yellow]●[/yellow] " if pnl is None else ""
        pt.add_row(t["time"], f"{mark}{t['symbol']}", f"{t['qty']:.0f}", f"{t['entry']:.2f}",
                   f"{t['exit']:.2f}" if t["exit"] else "—", pnl_str)
    if not trades:
        pt.add_row("—", "[dim]no trades today yet[/dim]", "—", "—", "—", "—")
    elif len(closed) > budget:
        pt.add_row("", f"[dim]+{len(closed) - budget} earlier closed[/dim]", "", "", "", "")

    # risk: collapse consecutive identical fires (e.g. 4× the same naked-stop exit) into
    # one ×N row, then cap at 3 — a repeating alert must not flood the board.
    risk = con.execute("SELECT timestamp, message FROM events WHERE event_type='risk_rule_triggered' "
                       "ORDER BY timestamp DESC LIMIT 12").fetchall()
    rk = Table(title="RISK / CIRCUIT BREAKER  [dim](last 3)[/dim]", box=box.SIMPLE)
    rk.add_column("time", style="cyan"); rk.add_column("rule", overflow="fold")
    deduped: list = []
    for ts, m in risk:
        if deduped and deduped[-1][1] == m:
            deduped[-1][2] += 1
        else:
            deduped.append([ts, m, 1])
    for ts, m, cnt in deduped[:3]:
        rule = m or ""
        if cnt > 1:
            rule += f" [dim]×{cnt}[/dim]"
        rk.add_row(str(ts)[11:19], rule)

    n_eval = len(_latest_eval_board(con))
    # component-attribution pulse: today's signal timing (early = inside the
    # confirmation window, the only +EV ones) + what each gate cut. Two tiny
    # date-scoped queries; full history lives at /api/attribution + the CLI.
    from datetime import date as _date
    _today = _date.today().isoformat()
    gates: dict = {}
    for (pj,) in con.execute(
            "SELECT payload_json FROM events WHERE event_type='risk_rule_triggered' "
            "AND timestamp >= ?", [_today]).fetchall():
        rt = (json.loads(pj or "{}").get("rule_type")) or "?"
        gates[rt] = gates.get(rt, 0) + 1
    early = late = 0
    for (ts,) in con.execute(
            "SELECT timestamp FROM events WHERE event_type='signal_ready' "
            "AND timestamp >= ?", [_today]).fetchall():
        m = (ts.hour - 9) * 60 + ts.minute - 30
        early, late = (early + 1, late) if 0 <= m <= 15 else (early, late + 1)
    gate_bits = ", ".join(f"{k}×{v}" for k, v in sorted(gates.items(), key=lambda x: -x[1])[:4])
    attrib_line = (f"\n[dim]signals {early} early / {late} late · gates: "
                   f"{gate_bits or 'none yet'}[/dim]")
    # naked/under-protected alarm — the failure that cost us this session and the
    # board never showed. A red banner the instant a held long lacks stop coverage.
    naked = _unprotected_positions(con)
    naked_line = ""
    if naked:
        names = ", ".join(f"{n['symbol']}({n['protected']:.0f}/{n['qty']:.0f})" for n in naked)
        naked_line = (f"\n[bold white on red] ⚠ UNPROTECTED: {names} — held long with no/short "
                      f"protective stop [/]")
    header = Panel(
        f"[bold]momentum live[/bold]   "
        f"[{pcolor}]{phase}[/{pcolor}] [dim]{detail}[/dim]   "
        f"equity [green]{eq}[/green]   "
        f"day P&L [bold {dcol}]{day_pnl:+,.0f}[/]   "
        f"[{hb_color}]♥ {hb_text}[/{hb_color}]   "
        f"[bold cyan]{armed_n} armed[/bold cyan] · {n_eval} scan"
        f"{attrib_line}{naked_line}",
        style="red" if naked else "cyan")
    # order = safety-first: header (protected? green? live?) → armed (about to fire?) →
    # risk/circuit-breaker (promoted above trades, it's a safety surface) → trades log.
    return Group(header, tt, rk, pt)


@app.command()
def journal():
    """Print the day's trade journal (today's trades + day P&L) — for the close
    capture and the nightly record."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:  # noqa: BLE001
        today = datetime.now().date().isoformat()
    con = _con()
    trades, realized = _todays_trades(con)
    day_pnl = _day_pnl(con)
    open_unreal = _open_unrealized(con)
    matched = _matched_realized(day_pnl, open_unreal, realized)
    console.print(f"[bold]=== trade journal {today} ===[/bold]")
    if day_pnl is not None:
        console.print(f"day P&L: [{'green' if day_pnl >= 0 else 'red'}]{day_pnl:+,.0f}[/]"
                      f"  ([dim]matched {matched:+,.0f} · open {open_unreal:+,.0f}[/])")
    if not trades:
        console.print("no trades today.")
        return
    t = Table(box=box.SIMPLE)
    for c in ("time", "symbol", "qty", "entry", "exit", "P&L", "status"):
        t.add_column(c, justify="left" if c in ("symbol", "status") else "right")
    for tr in sorted(trades, key=lambda x: x["time"]):
        pnl = tr["pnl"]
        t.add_row(tr["time"], str(tr["symbol"]), f"{tr['qty']:.0f}", f"{tr['entry']:.2f}",
                  f"{tr['exit']:.2f}" if tr["exit"] else "—",
                  f"{pnl:+.0f}" if pnl is not None else "—", tr["status"])
    console.print(t)
    closed = [tr for tr in trades if tr["status"] == "closed"]
    if closed:
        wins = sum(1 for tr in closed if (tr["pnl"] or 0) > 0)
        console.print(f"[dim]{len(closed)} closed · {wins} win ({wins / len(closed) * 100:.0f}%) · "
                      f"{len(trades) - len(closed)} still open[/dim]")


@app.command()
def watch(interval: float = 3.0, once: bool = typer.Option(False, help="render one frame and exit")):
    """LIVE board: the symbols being evaluated + their determinations, refreshing."""
    import time as _t
    from rich.live import Live
    con = _con()
    if once:
        console.print(_render_board(con))
        return
    from rich.panel import Panel
    try:
        with Live(_render_board(con), console=console, screen=True, refresh_per_second=2) as live:
            while True:
                _t.sleep(max(0.5, interval))
                try:
                    live.update(_render_board(con))
                except Exception as exc:  # noqa: BLE001
                    # a transient DB hiccup must not kill the board — rebuild the
                    # connection and keep refreshing
                    try:
                        con = _con()
                    except Exception:  # noqa: BLE001
                        pass
                    live.update(Panel(f"[yellow]reconnecting… ({str(exc)[:70]})[/yellow]",
                                      style="yellow"))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    app()
