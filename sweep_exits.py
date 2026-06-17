#!/usr/bin/env python3
"""Sweep trade-EXIT variants over the stored gapper sessions.

Entry params are held FIXED at the live ORB config (so we isolate the effect of
the exit rules), and each variant backtests the SAME shared exit logic the live
manager uses (strategy/exits.py) — so a variant that wins here transfers live.

Sessions are split by date into TRAIN (earlier) and TEST (later) so a winner can
be checked out-of-sample (guards against overfitting to a handful of days).

    PYTHONPATH=. python sweep_exits.py --list-names
    PYTHONPATH=. python sweep_exits.py --split train --json
    PYTHONPATH=. python sweep_exits.py --split test --names static_2R,be1_t3 --json
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter

from dotenv import load_dotenv

from config import Config
from research.multi_schema import open_research_db
from strategy.backtest.engine import BacktestEngine
from strategy.exits import ExitConfig, TRAIL_PRIOR_LOW, TRAIL_PCT, simulate_exit
from strategy.models import SetupCriteria

load_dotenv()
os.environ["STRATEGY_SETUPS"] = "opening_range_break"  # isolate the ORB

# Fixed ORB entry config (mirrors the live learned params) so only EXITS vary.
ENTRY = dict(warmup=3, eval_every=1, ready=55, min_bars=3, gap_min=0.05, rvol_min=2.0)


def variants() -> dict[str, ExitConfig]:
    """Named exit variants to sweep. Keep this the single grid definition."""
    v: dict[str, ExitConfig] = {}
    # --- fixed bracket targets (the current style) ---
    for r in (1.5, 2.0, 2.5, 3.0):
        v[f"static_{r:g}R"] = ExitConfig(target_r=r)
    # --- move to breakeven, then let a far target run ---
    for be in (0.5, 0.75, 1.0):
        v[f"be{be:g}_t3"] = ExitConfig(target_r=3.0, breakeven_at_r=be)
    # --- trail under prior-bar lows (target far so the trail is the exit) ---
    for after in (0.5, 1.0, 1.5):
        v[f"trailLow_a{after:g}"] = ExitConfig(
            target_r=10.0, trail_mode=TRAIL_PRIOR_LOW, trail_after_r=after)
    # --- percent trailing off the high-water mark ---
    for pct in (0.04, 0.06, 0.10):
        v[f"trailPct{int(pct*100)}"] = ExitConfig(
            target_r=10.0, trail_mode=TRAIL_PCT, trail_pct=pct, trail_after_r=1.0)
    # --- scale out into strength, run the rest ---
    v["scale50_1R_t3"] = ExitConfig(target_r=3.0, scale_out_r=1.0, scale_out_pct=0.5)
    v["scale50_1.5R_t4"] = ExitConfig(target_r=4.0, scale_out_r=1.5, scale_out_pct=0.5)
    v["scale33_1R_trailLow"] = ExitConfig(
        target_r=10.0, scale_out_r=1.0, scale_out_pct=0.34,
        trail_mode=TRAIL_PRIOR_LOW, trail_after_r=1.0)
    # --- first red candle exits ---
    v["firstRed_t3"] = ExitConfig(target_r=3.0, first_red_exit=True, trail_after_r=0.0)
    v["be1_firstRed_t4"] = ExitConfig(
        target_r=4.0, breakeven_at_r=1.0, first_red_exit=True, trail_after_r=1.0)
    # --- composites: breakeven + trail (+ scale) ---
    v["be1_trailLow_t4"] = ExitConfig(
        target_r=4.0, breakeven_at_r=1.0, trail_mode=TRAIL_PRIOR_LOW, trail_after_r=1.0)
    v["scale50_be1_trailLow"] = ExitConfig(
        target_r=10.0, breakeven_at_r=1.0, scale_out_r=1.0, scale_out_pct=0.5,
        trail_mode=TRAIL_PRIOR_LOW, trail_after_r=1.0)
    return v


def load_split(con, which: str):
    """Return (train, test, all) session lists split by date (70/30 by day)."""
    from learn_params import load_sessions
    sessions = load_sessions(con)
    days = sorted({s for _, s, _, _, _ in sessions})
    cut = days[int(len(days) * 0.7)] if len(days) >= 4 else (days[-1] if days else None)
    train = [s for s in sessions if s[1] < cut] if cut else sessions
    test = [s for s in sessions if s[1] >= cut] if cut else []
    if which == "train":
        return train or sessions
    if which == "test":
        return test or sessions
    return sessions


def extract_entries(sessions) -> list[tuple]:
    """Run the entry detection ONCE (with a reference static exit) and return the
    fixed entry set [(entry_price, init_stop, qty, bars_after), ...]. Every exit
    variant is then scored against the SAME entries — far faster than re-running
    the expensive per-bar evaluation per variant, and the fair comparison (same
    trades, only the management differs).
    """
    ref = BacktestEngine(
        Config(), target_r=2.0, warmup_bars=ENTRY["warmup"],
        eval_every=ENTRY["eval_every"], ready_score_pct=ENTRY["ready"],
        min_bars=ENTRY["min_bars"], exit_config=ExitConfig(target_r=2.0),
        criteria=SetupCriteria(gap_pct_min=ENTRY["gap_min"],
                               relative_volume_min=ENTRY["rvol_min"]),
    )
    entries: list[tuple] = []
    for symbol, _sess, bars, pc, adv in sessions:
        bars_r = bars.reset_index(drop=True)
        for t in ref.run(bars, symbol, previous_close=pc, avg_daily_volume=adv).trades:
            if t.entry_index is None:
                continue
            bars_after = bars_r.iloc[t.entry_index + 1:]
            entries.append((t.entry_price, t.stop_price, t.quantity, bars_after))
    return entries


def run_variant(entries, cfg: ExitConfig) -> dict:
    rs, pnls, reasons = [], [], Counter()
    for entry, stop, qty, bars_after in entries:
        risk = entry - stop
        if risk <= 0:
            continue
        res = simulate_exit(entry, stop, bars_after, cfg)
        rs.append(res.r_multiple)
        pnls.append(res.r_multiple * risk * qty)   # gross; costs ~const across variants
        for f in res.fills:
            reasons[f.reason] += 1
    n = len(rs)
    if not n:
        return {"trades": 0, "win": 0.0, "pnl": 0.0, "avgR": 0.0, "reasons": {}}
    return {
        "trades": n,
        "win": round(sum(1 for r in rs if r > 0) / n, 3),
        "pnl": round(sum(pnls), 0),
        "avgR": round(sum(rs) / n, 3),
        "reasons": dict(reasons),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="all", choices=["train", "test", "all"])
    ap.add_argument("--names", default="", help="comma-separated variant names (default: all)")
    ap.add_argument("--list-names", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    grid = variants()
    if args.list_names:
        print("\n".join(grid))
        return

    con = open_research_db("market")
    sessions = load_split(con, args.split)
    entries = extract_entries(sessions)
    names = [s.strip() for s in args.names.split(",") if s.strip()] or list(grid)
    out = []
    for name in names:
        cfg = grid.get(name)
        if cfg is None:
            continue
        m = run_variant(entries, cfg)
        m["name"] = name
        m["desc"] = cfg.describe()
        out.append(m)
    out.sort(key=lambda m: m["avgR"], reverse=True)

    if args.json:
        print(json.dumps({"split": args.split, "n_sessions": len(sessions),
                          "n_entries": len(entries), "results": out}))
        return
    print(f"=== exit sweep ({args.split}, {len(sessions)} sessions, {len(entries)} entries) ===")
    print(f"{'variant':22s} {'trades':>6} {'win%':>5} {'avgR':>6} {'pnl':>9}  desc")
    for m in out:
        print(f"{m['name']:22s} {m['trades']:>6} {m['win']*100:>4.0f}% {m['avgR']:>+6.3f} "
              f"{m['pnl']:>9.0f}  {m['desc']}")


if __name__ == "__main__":
    main()
