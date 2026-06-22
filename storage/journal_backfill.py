"""Reconstruct completed round-trips from Alpaca's filled-order history.

The live trade journal (position_closed events) only began at the snapshot-diff
reconcile in alpaca_paper/sync.py, so it's forward-only. This rebuilds the PAST:
it walks every filled order (parent + bracket legs), pairs entries with exits per
symbol into episodes (open -> back to flat), and yields one closed round-trip per
episode — timestamped at the exit fill so each lands on its own market day.

Pure functions here; the CLI wrapper is scripts-level (backfill_journal.py).
"""

from __future__ import annotations

from collections import defaultdict

# Alpaca order type -> trade-journal exit reason (mirrors alpaca_paper/sync.py)
_EXIT_REASON = {
    "stop": "stop_loss", "stop_limit": "stop_loss", "trailing_stop": "trailing_stop",
    "limit": "take_profit", "market": "market_exit",
}


def flatten_fills(raw_orders: list[dict]) -> list[dict]:
    """Extract FILLED fills (parent orders + bracket legs) as flat dicts.
    Child legs don't repeat the symbol — inherit it from the parent."""
    fills: list[dict] = []

    def add(o: dict, parent: dict | None = None) -> None:
        sym = o.get("symbol") or (parent or {}).get("symbol")
        px = o.get("filled_avg_price")
        when = o.get("filled_at")
        if str(o.get("status")) != "filled" or px in (None, "") or not when or not sym:
            return
        try:
            fills.append({
                "symbol": sym,
                "side": str(o.get("side") or "").lower(),
                "qty": float(o.get("filled_qty") or 0),
                "price": float(px),
                "time": when,
                "type": str(o.get("type") or ""),
                "stop_price": float(o["stop_price"]) if o.get("stop_price") not in (None, "") else None,
            })
        except (TypeError, ValueError):
            pass

    for o in raw_orders or []:
        add(o)
        for leg in (o.get("legs") or []):
            add(leg, parent=o)
    return fills


def reconstruct_round_trips(fills: list[dict]) -> list[dict]:
    """Pair fills into completed round-trips per symbol. A round-trip opens when
    the running position leaves flat and closes when it returns to flat; only
    closed ones are returned (still-open positions are skipped). Realized P&L is
    matched proceeds-minus-cost over the episode (avg entry/exit). Returns dicts:
    {symbol, qty, entry_price, exit_price, realized_pnl, exit_time, exit_reason,
     stop_loss_price, side}."""
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for f in fills:
        if f.get("qty", 0) > 0 and f.get("side") in ("buy", "sell"):
            by_sym[f["symbol"]].append(f)

    trips: list[dict] = []
    for sym, fs in by_sym.items():
        fs.sort(key=lambda x: str(x["time"]))
        pos = 0.0            # signed open quantity
        direction = 0       # +1 long episode, -1 short episode
        ent_qty = ent_cost = ex_qty = ex_proceeds = 0.0
        last_exit: dict | None = None
        stop_level = None
        for f in fs:
            signed = f["qty"] if f["side"] == "buy" else -f["qty"]
            if abs(pos) < 1e-9:                       # opening a fresh episode
                direction = 1 if signed > 0 else -1
                ent_qty = ent_cost = ex_qty = ex_proceeds = 0.0
                last_exit = None
                stop_level = None
            if f.get("stop_price"):
                stop_level = f["stop_price"]
            is_entry = (signed > 0) == (direction > 0)
            if is_entry:
                ent_qty += f["qty"]
                ent_cost += f["qty"] * f["price"]
            else:
                ex_qty += f["qty"]
                ex_proceeds += f["qty"] * f["price"]
                last_exit = f
            pos += signed
            if abs(pos) < 1e-9 and ent_qty > 0 and last_exit is not None:
                entry = ent_cost / ent_qty if ent_qty else 0.0
                exit_px = ex_proceeds / ex_qty if ex_qty else 0.0
                realized = (ex_proceeds - ent_cost) if direction > 0 else (ent_cost - ex_proceeds)
                trips.append({
                    "symbol": sym,
                    "qty": round(min(ent_qty, ex_qty), 4),
                    "entry_price": round(entry, 4),
                    "exit_price": round(exit_px, 4),
                    "realized_pnl": round(realized, 2),
                    "exit_time": last_exit["time"],
                    "exit_reason": _EXIT_REASON.get(last_exit["type"], "closed"),
                    "stop_loss_price": round(stop_level, 4) if stop_level else None,
                    "side": "buy" if direction > 0 else "sell",
                })
    trips.sort(key=lambda t: str(t["exit_time"]))
    return trips
