#!/usr/bin/env python3
"""Diagnose what separates WINNING ORB trades from losers, to find filters that
raise the edge. Runs the live entry+exit config over the stored gapper sessions
and buckets each trade's realized R by feature (stop distance, entry time, gap,
rvol, how-extended-at-entry, price). Big avgR gaps between buckets = a filter.

    PYTHONPATH=. python diagnose_entries.py
"""
from __future__ import annotations

import os
import sys

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, ".")
load_dotenv()
os.environ["STRATEGY_SETUPS"] = "opening_range_break"

from config import Config
from learn_params import load_sessions
from research.multi_schema import open_research_db
from strategy.backtest.engine import BacktestEngine
from strategy.exits import ExitConfig, TRAIL_PRIOR_LOW, parse_profit_tiers
from strategy.models import SetupCriteria


def main():
    con = open_research_db("market")
    sessions = load_sessions(con)
    cfg = ExitConfig(target_r=10.0, trail_mode=TRAIL_PRIOR_LOW, trail_after_r=1.0,
                     profit_lock_tiers=parse_profit_tiers("8:3,15:9,25:18,40:30"))
    eng = BacktestEngine(Config(), target_r=10.0, warmup_bars=3, eval_every=1,
                         ready_score_pct=55, min_bars=3, exit_config=cfg,
                         criteria=SetupCriteria(gap_pct_min=0.05, relative_volume_min=2.0))
    trades = []
    for sym, _sess, bars, pc, adv in sessions:
        bars_r = bars.reset_index(drop=True)
        if not len(bars_r):
            continue
        first_open = float(bars_r["open"].iloc[0])
        gap = (first_open / pc - 1) if pc else 0.0
        rvol = (float(bars_r["volume"].sum()) / adv) if adv else 0.0
        t0 = pd.Timestamp(bars_r["timestamp"].iloc[0])
        for t in eng.run(bars, sym, previous_close=pc, avg_daily_volume=adv).trades:
            entry, stop = t.entry_price, t.stop_price
            try:
                em = (pd.Timestamp(t.entry_time) - t0).total_seconds() / 60.0
            except Exception:
                em = float("nan")
            trades.append({
                "r": t.r_multiple or 0.0,
                "stop_dist": (entry - stop) / entry * 100 if entry else 0.0,
                "em": em,
                "gap": gap * 100,
                "rvol": rvol,
                "ext": (entry / first_open - 1) * 100 if first_open else 0.0,
                "price": entry,
            })

    n = len(trades)
    avg = sum(t["r"] for t in trades) / n if n else 0
    print(f"total trades: {n}   overall avgR: {avg:+.3f}   sumR: {sum(t['r'] for t in trades):+.1f}\n")

    def bucket(name, key, edges):
        print(f"=== avgR by {name} ===")
        print(f"  {'bucket':>13} {'n':>4} {'win%':>5} {'avgR':>8} {'sumR':>8}")
        for lo, hi in edges:
            grp = [t for t in trades if t[key] == t[key] and lo <= t[key] < hi]
            if not grp:
                continue
            rs = [t["r"] for t in grp]
            print(f"  {f'[{lo:g},{hi:g})':>13} {len(grp):>4} "
                  f"{sum(1 for r in rs if r > 0) / len(rs) * 100:>4.0f}% "
                  f"{sum(rs) / len(rs):>+8.3f} {sum(rs):>+8.1f}")
        print()

    bucket("STOP DISTANCE %", "stop_dist", [(0, 5), (5, 10), (10, 15), (15, 25), (25, 100)])
    bucket("ENTRY MINUTE (after open)", "em", [(0, 10), (10, 20), (20, 40), (40, 90), (90, 400)])
    bucket("GAP %", "gap", [(0, 10), (10, 30), (30, 60), (60, 150), (150, 2000)])
    bucket("RELATIVE VOLUME", "rvol", [(0, 5), (5, 15), (15, 50), (50, 5000)])
    bucket("EXTENSION above open % at entry", "ext", [(-100, 0), (0, 5), (5, 15), (15, 50), (50, 2000)])
    bucket("PRICE $", "price", [(0, 2), (2, 5), (5, 10), (10, 20)])


if __name__ == "__main__":
    main()
