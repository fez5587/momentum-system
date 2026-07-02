"""Per-component performance attribution — rate each pipeline stage from the event stream.

The owner's modularity ask: "isolate or add monitoring to specific components to rate or
increase performance." Every stage already emits events (event-sourced architecture), so this
derives a per-stage scorecard without touching the pipeline:

  DISCOVERY  — symbols found/day, and what fraction ever produced a ready signal (precision)
  SIGNALS    — ready signals/day, split EARLY (within the confirmation window) vs LATE
  GATES      — what each entry gate cut (risk_rule_triggered histogram by rule_type)
  EXECUTION  — approvals -> broker submissions -> fills; backout rate (entry efficiency)
  OUTCOMES   — realized P&L, win rate, avg win/loss, trades/day (the only stage that pays)

CLI:  python -m research.attribution [--days 14]
API:  api/main.py serves compute_attribution() at /api/attribution for the dashboard.

All queries filter on the native `timestamp` column first (payload_json casts on the full
676k-row events table time out — learned the hard way)."""

import argparse
import os
from datetime import date, timedelta
from urllib.parse import urlparse

import psycopg2

try:                                                # optional; .env for CLI use
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:  # noqa: BLE001
    pass

CONFIRM_MINUTE_DEFAULT = 15   # mirror TRADING_ENTRY_CONFIRM_BY_MINUTE for the early/late split


def _connect():
    u = urlparse(os.environ["DATABASE_URL"])
    return psycopg2.connect(host=u.hostname, port=u.port, user=u.username,
                            password=u.password, dbname=u.path.lstrip("/"))


def compute_attribution(days: int = 14, confirm_minute: int | None = None) -> dict:
    """Per-day, per-stage metrics for the last `days` calendar days. Pure read."""
    confirm_minute = confirm_minute or int(
        os.getenv("TRADING_ENTRY_CONFIRM_BY_MINUTE", str(CONFIRM_MINUTE_DEFAULT)))
    since = (date.today() - timedelta(days=days)).isoformat()
    out: dict = {"since": since, "confirm_minute": confirm_minute, "days": {}}
    cx = _connect()
    cur = cx.cursor()

    def day_of(ts):
        return str(ts)[:10]

    def bump(d, stage, key, n=1):
        out["days"].setdefault(d, {}).setdefault(stage, {})
        out["days"][d][stage][key] = out["days"][d][stage].get(key, 0) + n

    # DISCOVERY + SIGNALS + EXECUTION counts (cheap: event_type + timestamp only)
    cur.execute(
        """SELECT event_type, timestamp FROM public.events
           WHERE timestamp >= %s AND event_type IN
             ('symbol_discovered','signal_ready','order_approval_requested',
              'order_approved','order_submitted','order_cancelled','order_rejected')""",
        (since,))
    for et, ts in cur.fetchall():
        bump(day_of(ts), "counts", et)

    # SIGNALS early/late split (uses event wall-clock vs the 09:30 open)
    cur.execute("SELECT timestamp FROM public.events WHERE timestamp >= %s AND event_type='signal_ready'",
                (since,))
    for (ts,) in cur.fetchall():
        minute = (ts.hour - 9) * 60 + ts.minute - 30
        bump(day_of(ts), "signals", "early" if 0 <= minute <= confirm_minute else "late")

    # GATES histogram (needs payload rule_type; scoped by type+time so it stays fast)
    cur.execute(
        """SELECT timestamp, payload_json::jsonb->>'rule_type' FROM public.events
           WHERE timestamp >= %s AND event_type='risk_rule_triggered'""", (since,))
    for ts, rule in cur.fetchall():
        bump(day_of(ts), "gates", rule or "?")

    # OUTCOMES: realized P&L per close
    cur.execute(
        """SELECT timestamp, payload_json::jsonb->>'realized_pnl',
                  payload_json::jsonb->>'symbol'
           FROM public.events WHERE timestamp >= %s AND event_type='position_closed'""",
        (since,))
    for ts, pnl, sym in cur.fetchall():
        d = day_of(ts)
        try:
            v = float(pnl)
        except (TypeError, ValueError):
            continue
        st = out["days"].setdefault(d, {}).setdefault("outcomes", {})
        st["trades"] = st.get("trades", 0) + 1
        st["pnl"] = round(st.get("pnl", 0.0) + v, 2)
        st["wins"] = st.get("wins", 0) + (1 if v > 0 else 0)
        st.setdefault("closes", []).append({"symbol": sym, "pnl": round(v, 2)})
    cx.close()

    # stage RATINGS per day (0-100-ish, honest heuristics, documented inline)
    for d, st in out["days"].items():
        c = st.get("counts", {})
        disc = c.get("symbol_discovered", 0)
        ready = c.get("signal_ready", 0)
        subm = c.get("order_submitted", 0)
        canc = c.get("order_cancelled", 0)
        oc = st.get("outcomes", {})
        trades = oc.get("trades", 0)
        rating = {}
        # discovery precision: what fraction of discovered names produced a ready signal
        rating["discovery_precision_pct"] = round(100 * min(1.0, ready / disc), 0) if disc else None
        # signal timing: fraction of ready signals inside the tradeable window
        sg = st.get("signals", {})
        tot_sig = sg.get("early", 0) + sg.get("late", 0)
        rating["signals_early_pct"] = round(100 * sg.get("early", 0) / tot_sig, 0) if tot_sig else None
        # execution efficiency: submissions that didn't get cancelled/backed out
        rating["fill_efficiency_pct"] = round(100 * max(0, subm - canc) / subm, 0) if subm else None
        # outcomes: the ground truth
        rating["win_rate_pct"] = round(100 * oc.get("wins", 0) / trades, 0) if trades else None
        rating["pnl"] = oc.get("pnl")
        st["rating"] = rating
    return out


def render(att: dict) -> str:
    lines = [f"=== component attribution since {att['since']} "
             f"(confirm window {att['confirm_minute']}m) ==="]
    hdr = (f"{'day':11}{'disc':>5}{'ready':>6}{'early%':>7}{'gate-cuts':>10}"
           f"{'subm':>5}{'fill%':>6}{'trades':>7}{'win%':>6}{'P&L':>9}")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for d in sorted(att["days"]):
        st = att["days"][d]
        c, r = st.get("counts", {}), st.get("rating", {})
        gates = sum(st.get("gates", {}).values())
        lines.append(
            f"{d:11}{c.get('symbol_discovered',0):>5}{c.get('signal_ready',0):>6}"
            f"{str(r.get('signals_early_pct','-')):>7}{gates:>10}"
            f"{c.get('order_submitted',0):>5}{str(r.get('fill_efficiency_pct','-')):>6}"
            f"{st.get('outcomes',{}).get('trades',0):>7}{str(r.get('win_rate_pct','-')):>6}"
            f"{('$%+.0f' % r['pnl']) if r.get('pnl') is not None else '-':>9}")
        g = st.get("gates", {})
        if g:
            lines.append("           gates: " + ", ".join(f"{k}x{v}" for k, v in
                                                          sorted(g.items(), key=lambda x: -x[1])))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="per-component pipeline attribution")
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()
    print(render(compute_attribution(days=args.days)))


if __name__ == "__main__":
    main()
