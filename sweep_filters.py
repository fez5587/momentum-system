#!/usr/bin/env python3
"""Sweep ENTRY filters (price cap / volume floor / gap cap) train-vs-test.

The diagnostic showed clear edges — cheap stocks, high relative volume, and
non-blowoff gaps win; $10-20 names lose. This verifies which filter combo lifts
the out-of-sample expectancy without overfitting. Entries+exit are the verified
live config; only the universe selection (which symbol-days qualify) varies.

Fast: entries are extracted ONCE (with their price/gap/rvol tagged), then each
filter combo just selects from that list — no re-running the engine per combo.

    PYTHONPATH=. python sweep_filters.py            # ranked table (train+test)
    PYTHONPATH=. python sweep_filters.py --json     # for the verify workflow
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys

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
from sweep_exits import ENTRY, run_variant

CFG = ExitConfig(target_r=10.0, trail_mode=TRAIL_PRIOR_LOW, trail_after_r=1.0,
                 profit_lock_tiers=parse_profit_tiers("8:3,15:9,25:18,40:30"))

GRID = {
    "price_max": [5.0, 7.0, 10.0, 20.0],
    "rvol_min": [2.0, 4.0, 6.0],
    "gap_max": [60.0, 120.0, 5000.0],   # 5000 = off
}
BASE = {"price_max": 20.0, "rvol_min": 2.0, "gap_max": 5000.0}


def extract_with_features(sessions):
    """[{entry, stop, qty, ba, price, gap, rvol}] — engine run ONCE per session."""
    ref = BacktestEngine(
        Config(), target_r=2.0, warmup_bars=ENTRY["warmup"], eval_every=ENTRY["eval_every"],
        ready_score_pct=ENTRY["ready"], min_bars=ENTRY["min_bars"],
        exit_config=ExitConfig(target_r=2.0),
        criteria=SetupCriteria(gap_pct_min=ENTRY["gap_min"], relative_volume_min=ENTRY["rvol_min"]))
    out = []
    for sym, _sess, bars, pc, adv in sessions:
        br = bars.reset_index(drop=True)
        if not len(br):
            continue
        gap = (float(br["open"].iloc[0]) / pc - 1) * 100 if pc else 0.0
        rvol = (float(br["volume"].sum()) / adv) if adv else 0.0
        for t in ref.run(bars, sym, previous_close=pc, avg_daily_volume=adv).trades:
            if t.entry_index is None:
                continue
            out.append({"entry": t.entry_price, "stop": t.stop_price, "qty": t.quantity,
                        "ba": br.iloc[t.entry_index + 1:], "gap": gap, "rvol": rvol})
    return out


def run_combo(feats, p):
    sel = [(f["entry"], f["stop"], f["qty"], f["ba"]) for f in feats
           if 1.0 <= f["entry"] <= p["price_max"] and 3.0 <= f["gap"] <= p["gap_max"]
           and f["rvol"] >= p["rvol_min"]]
    if not sel:
        return {"trades": 0, "win": 0.0, "avgR": 0.0, "pnl": 0.0}
    return run_variant(sel, CFG)


def split_by_date(sessions, which):
    days = sorted({s[1] for s in sessions})
    cut = days[int(len(days) * 0.7)] if len(days) >= 4 else (days[-1] if days else None)
    if which == "train":
        return [s for s in sessions if cut and s[1] < cut] or sessions
    if which == "test":
        return [s for s in sessions if cut and s[1] >= cut]
    return sessions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    con = open_research_db("market")
    sessions = load_sessions(con)
    feat_train = extract_with_features(split_by_date(sessions, "train"))
    feat_test = extract_with_features(split_by_date(sessions, "test"))

    rows = []
    for c in itertools.product(*GRID.values()):
        p = dict(zip(GRID, c))
        tr, te = run_combo(feat_train, p), run_combo(feat_test, p)
        rows.append({
            "filter": f"px<={p['price_max']:g} rvol>={p['rvol_min']:g} gap<={p['gap_max']:g}",
            "params": p,
            "train_avgR": tr["avgR"], "train_trades": tr["trades"],
            "test_avgR": te["avgR"], "test_trades": te["trades"], "test_win": te["win"],
        })
    rows.sort(key=lambda r: r["test_avgR"], reverse=True)

    if args.json:
        print(json.dumps({"results": rows}))
        return
    base = run_combo(feat_test, BASE)
    base_tr = run_combo(feat_train, BASE)
    print(f"BASELINE (px<=20 rvol>=2 gap-off): train {base_tr['avgR']:+.3f} ({base_tr['trades']}t)  "
          f"TEST {base['avgR']:+.3f} ({base['trades']}t) win {base['win'] * 100:.0f}%\n")
    print(f"{'filter':>32} | {'train avgR':>10} {'n':>4} | {'TEST avgR':>10} {'n':>4} {'win%':>5}")
    for r in rows[:12]:
        print(f"{r['filter']:>32} | {r['train_avgR']:>+10.3f} {r['train_trades']:>4} | "
              f"{r['test_avgR']:>+10.3f} {r['test_trades']:>4} {r['test_win'] * 100:>4.0f}%")


if __name__ == "__main__":
    main()
