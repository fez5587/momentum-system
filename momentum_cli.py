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
import sys
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
        return ("loop signal seen", "green")
    color = "green" if secs < 15 else ("yellow" if secs < 45 else "red")
    return (f"loop {int(secs)}s ago", color)


_STATE_STYLE = {
    "armed": ("bold cyan", "● ARMED"),
    "fired": ("bold magenta", "▶ FIRED"),
    "filled": ("bold green", "◆ LONG"),
    "waiting": ("dim", "· waiting"),
    "weak": ("dim yellow", "  weak"),
}


def _todays_trades(con):
    """Reconstruct TODAY's round-trip trades (FIFO buy->sell per symbol) from the
    latest broker orders snapshot, plus still-open lots. Resets each day (filters
    to today's fills). Returns (trades, realized_pnl)."""
    from collections import defaultdict, deque
    from datetime import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        today = _dt.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:  # noqa: BLE001
        today = _dt.now().date().isoformat()
    rows = con.execute("SELECT payload_json FROM events WHERE event_type='account_orders_updated' "
                       "ORDER BY timestamp DESC LIMIT 1").fetchall()
    if not rows:
        return [], 0.0
    orders = json.loads(rows[0][0]).get("orders") or []
    fills = [o for o in orders
             if float(o.get("filled_quantity") or 0) > 0
             and str(o.get("submitted_at") or "")[:10] == today]
    fills.sort(key=lambda o: o.get("submitted_at") or "")
    lots: dict = defaultdict(deque)
    trades = []
    for o in fills:
        sym, side = o.get("symbol"), o.get("side")
        qty = float(o.get("filled_quantity") or 0)
        px = float(o.get("filled_avg_price") or 0)
        tm = str(o.get("submitted_at") or "")[11:19]
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

    # --- the 6 armed triggers: the focused, live watchlist ------------------
    tt = Table(box=box.SIMPLE_HEAVY, expand=True,
               title="armed triggers — the most-promising gappers, ready to fire")
    for c, j in (("symbol", "left"), ("state", "left"), ("price", "right"),
                 ("gap", "right"), ("rvol", "right"), ("trigger", "right"),
                 ("→trigger", "right"), ("stop", "right")):
        tt.add_column(c, justify=j, no_wrap=True)
    tt.add_column("catalyst", justify="left", overflow="ellipsis", max_width=34)
    for r in trig_rows:
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
    if not trig_rows:
        tt.add_row("[dim]—[/dim]", "[dim]waiting for the open / first bars[/dim]",
                   "—", "—", "—", "—", "—", "—", "—")

    sig = con.execute("SELECT timestamp, message FROM events WHERE event_type='signal_ready' "
                      "ORDER BY timestamp DESC LIMIT 5").fetchall()
    st = Table(title="ready signals", box=box.SIMPLE)
    st.add_column("time", style="cyan"); st.add_column("signal", overflow="fold")
    for ts, m in sig:
        st.add_row(str(ts)[11:19], m or "")

    pos_ev = con.execute("SELECT payload_json FROM events WHERE event_type='account_positions_updated' "
                         "ORDER BY timestamp DESC LIMIT 1").fetchall()
    open_unreal = 0.0
    if pos_ev:
        for p in (json.loads(pos_ev[0][0]).get("positions") or []):
            try:
                open_unreal += float(p.get("unrealized_pnl") if p.get("unrealized_pnl") is not None
                                     else p.get("unrealized_pl") or 0)
            except (TypeError, ValueError):
                pass
    trades, realized = _todays_trades(con)
    day_pnl = _day_pnl(con)
    if day_pnl is None:            # fallback if equity snapshots missing
        day_pnl = realized + open_unreal
    dcol = "green" if day_pnl >= 0 else "red"
    pt = Table(title=f"today's trades — day P&L [{dcol}]{day_pnl:+,.0f}[/] "
                     f"([dim]matched {realized:+,.0f} · open {open_unreal:+,.0f}[/])",
               box=box.SIMPLE)
    for c in ("time", "symbol", "qty", "entry", "exit", "P&L", ""):
        pt.add_column(c, justify="left" if c in ("symbol", "") else "right")
    for t in sorted(trades, key=lambda x: x["time"], reverse=True)[:14]:
        pnl = t["pnl"]
        if pnl is None:
            pnl_str, tag = "[dim]—[/dim]", "[yellow]● open[/yellow]"
        else:
            pnl_str = f"[{'green' if pnl >= 0 else 'red'}]{pnl:+.0f}[/]"
            tag = "closed"
        pt.add_row(t["time"], str(t["symbol"]), f"{t['qty']:.0f}", f"{t['entry']:.2f}",
                   f"{t['exit']:.2f}" if t["exit"] else "—", pnl_str, tag)
    if not trades:
        pt.add_row("—", "[dim]no trades today yet[/dim]", "—", "—", "—", "—", "—")

    risk = con.execute("SELECT timestamp, message FROM events WHERE event_type='risk_rule_triggered' "
                       "ORDER BY timestamp DESC LIMIT 4").fetchall()
    rk = Table(title="risk / circuit breaker", box=box.SIMPLE)
    rk.add_column("time", style="cyan"); rk.add_column("rule", overflow="fold")
    for ts, m in risk:
        rk.add_row(str(ts)[11:19], m or "")

    n_eval = len(_latest_eval_board(con))
    header = Panel(
        f"[bold]momentum live[/bold]   "
        f"[{pcolor}]{phase}[/{pcolor}] [dim]{detail}[/dim]   "
        f"equity [green]{eq}[/green]   "
        f"day P&L [bold {dcol}]{day_pnl:+,.0f}[/]   "
        f"[{hb_color}]♥ {hb_text}[/{hb_color}]   "
        f"[bold cyan]{armed_n} armed[/bold cyan] · {n_eval} scanning\n"
        f"[dim]hunting $1–20 gappers (≥ gap% on ≥ 2× volume); buy the break of the "
        f"opening-range high, stop at its low · Ctrl-C to exit[/dim]",
        style="cyan")
    return Group(header, tt, st, pt, rk)


@app.command()
def watch(interval: float = 3.0, once: bool = typer.Option(False, help="render one frame and exit")):
    """LIVE board: the symbols being evaluated + their determinations, refreshing."""
    import time as _t
    from rich.live import Live
    con = _con()
    if once:
        console.print(_render_board(con))
        return
    try:
        with Live(_render_board(con), console=console, screen=True, refresh_per_second=2) as live:
            while True:
                _t.sleep(max(0.5, interval))
                live.update(_render_board(con))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    app()
