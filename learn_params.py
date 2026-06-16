#!/usr/bin/env python3
"""learn_params.py — learn strategy parameters that enter EARLIER while staying
profitable, by back-testing a grid of them over the stored historical sessions.

The system is structurally 'late': the default warmup_bars=15 + evaluator
min_bars=10 + eval_every=5 mean it doesn't even look at a name until ~15 minutes
in and only checks every 5 — so small-cap moves that happen in the first 5-15
minutes are already over. This grid-searches those knobs (plus the
ready-score / gap / RVOL thresholds) against the minute bars in Postgres and
ranks param sets by total P&L AND by 'entry_min' (minutes after the 09:30 open
the average entry fires), so you can pick a config that catches more of the move.

    PYTHONPATH=. /home/philip/.venvs/momentum/bin/python learn_params.py

NOTE: quality of the 'learning' scales with how many historical RTH sessions are
in the DB. With only a day or two it is illustrative; backfill more days (or let
the live loop accumulate them) for a robust optimum.
"""

from __future__ import annotations

import itertools

import pandas as pd
from dotenv import load_dotenv

from config import Config
from research.multi_schema import open_research_db
from research.query import (
    query_avg_daily_volume,
    query_minute_bars,
    query_previous_close,
)
from strategy.backtest.engine import BacktestEngine
from strategy.models import SetupCriteria

load_dotenv()


def load_sessions(con):
    """[(symbol, session_date, rth_bars, prev_close, adv), ...] for stored data."""
    rows = con.execute(
        "SELECT DISTINCT symbol, session_date FROM minute_bars ORDER BY session_date, symbol"
    ).fetchall()
    out = []
    for symbol, sess in rows:
        bars = query_minute_bars(con, symbol, sess)
        if "is_regular_hours" in bars.columns:
            bars = bars[bars["is_regular_hours"] == True].reset_index(drop=True)  # noqa: E712
        if len(bars) < 20:
            continue
        pc = query_previous_close(con, symbol, sess)
        adv = query_avg_daily_volume(con, symbol, sess)
        # Mimic the LIVE scanner: only keep sessions that actually gapped up or
        # traded elevated volume. Otherwise the backtest trades non-movers the
        # live discovery would never surface, burying the real gappers' edge.
        try:
            gap = (float(bars["open"].iloc[0]) - pc) / pc if pc else 0.0
            day_rvol = (float(bars["volume"].sum()) / adv) if adv else 0.0
        except Exception:  # noqa: BLE001
            gap, day_rvol = 0.0, 0.0
        if not (gap >= 0.03 or day_rvol >= 2.0):
            continue
        out.append((symbol, sess, bars, pc, adv))
    return out


def _entry_min(bars, entry_time) -> float:
    """Minutes from the session's first (09:30) bar to the entry. Lower = earlier."""
    try:
        first = pd.Timestamp(bars["timestamp"].iloc[0])
        return max(0.0, (pd.Timestamp(entry_time) - first).total_seconds() / 60.0)
    except Exception:  # noqa: BLE001
        return float("nan")


def run_combo(sessions, *, warmup, min_bars, eval_every, ready, gap_min, rvol_min, target_r=2.0):
    eng = BacktestEngine(
        Config(), target_r=target_r, warmup_bars=warmup, eval_every=eval_every,
        ready_score_pct=ready, min_bars=min_bars,
        criteria=SetupCriteria(gap_pct_min=gap_min, relative_volume_min=rvol_min),
    )
    rs, pnls, ems = [], [], []
    for symbol, _sess, bars, pc, adv in sessions:
        for t in eng.run(bars, symbol, previous_close=pc, avg_daily_volume=adv).trades:
            rs.append(t.r_multiple or 0.0)
            pnls.append(t.realized_pnl or 0.0)
            ems.append(_entry_min(bars, t.entry_time))
    n = len(pnls)
    if not n:
        return {"trades": 0, "win": 0.0, "pnl": 0.0, "avgR": 0.0, "entry_min": float("nan")}
    valid_ems = [e for e in ems if e == e]
    return {
        "trades": n,
        "win": sum(1 for p in pnls if p > 0) / n,
        "pnl": sum(pnls),
        "avgR": sum(rs) / n,
        "entry_min": (sum(valid_ems) / len(valid_ems)) if valid_ems else float("nan"),
    }


def main():
    con = open_research_db("market")
    sessions = load_sessions(con)
    days = sorted({s for _, s, _, _, _ in sessions})
    print(f"loaded {len(sessions)} symbol-sessions over {len(days)} day(s): {days}")
    if len(days) < 3:
        print(f"NOTE: only {len(days)} day(s) of data — results are ILLUSTRATIVE. "
              "Backfill more RTH sessions for a robust optimum.\n")

    grid = {
        "warmup": [3, 6, 12],
        "min_bars": [3, 6],
        "eval_every": [1, 5],
        "ready": [55, 65],
        "gap_min": [0.05],
        "rvol_min": [2.0],
    }
    keys = list(grid)
    combos = [dict(zip(keys, c)) for c in itertools.product(*grid.values())]
    combos = [p for p in combos if p["warmup"] >= p["min_bars"]]
    print(f"sweeping {len(combos)} param sets over {len(sessions)} sessions...\n")

    results = [(p, run_combo(sessions, **p)) for p in combos]
    results.sort(key=lambda x: x[1]["pnl"], reverse=True)

    hdr = (f"{'warm':>4} {'minb':>4} {'evry':>4} {'rdy':>3} | "
           f"{'trades':>6} {'win%':>4} {'pnl':>9} {'avgR':>5} {'entry_min':>9}")
    print("=== top 8 by total P&L ===")
    print(hdr)
    for p, m in results[:8]:
        print(f"{p['warmup']:>4} {p['min_bars']:>4} {p['eval_every']:>4} {p['ready']:>3} | "
              f"{m['trades']:>6} {m['win']*100:>3.0f}% {m['pnl']:>9.0f} {m['avgR']:>5.2f} {m['entry_min']:>9.1f}")

    prof = sorted([(p, m) for p, m in results if m["pnl"] > 0 and m["trades"] >= 3],
                  key=lambda x: x[1]["entry_min"])
    print("\n=== earliest profitable (>=3 trades) — these enter soonest while still green ===")
    for p, m in prof[:5]:
        print(f"  entry@{m['entry_min']:>4.1f}min  warmup={p['warmup']} min_bars={p['min_bars']} "
              f"eval_every={p['eval_every']} ready={p['ready']}  ->  "
              f"pnl {m['pnl']:.0f}, win {m['win']*100:.0f}%, R {m['avgR']:.2f}, {m['trades']} trades")

    base = run_combo(sessions, warmup=15, min_bars=10, eval_every=5, ready=60, gap_min=0.05, rvol_min=2.5)
    print("\n=== current default (warmup15 / min10 / evry5 / ready60) ===")
    print(f"  pnl {base['pnl']:.0f}, win {base['win']*100:.0f}%, R {base['avgR']:.2f}, "
          f"{base['trades']} trades, entry@{base['entry_min']:.1f}min")


if __name__ == "__main__":
    main()
