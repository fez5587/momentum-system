#!/usr/bin/env python3
"""Nightly self-tuning loop.

Run after the close: backfill the latest bars, sweep the ORB strategy parameters
over the accumulated gapper sessions, and write the best config to
data/learned_params.json. run_live_paper.py reads that file on boot, so the
strategy re-tunes itself as data grows — no manual intervention.

    PYTHONPATH=. /home/philip/.venvs/momentum/bin/python nightly_tune.py

Only writes a config that is actually PROFITABLE on the sample (>0 P&L, >=5
trades); otherwise it leaves the existing learned_params.json untouched so a thin
or bad day can't degrade the live config.
"""

from __future__ import annotations

import itertools
import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from research.multi_schema import open_research_db

load_dotenv()


def main():
    # Tune the profitable setup only.
    os.environ["STRATEGY_SETUPS"] = "opening_range_break"

    # 1) accumulate the latest sessions (best-effort; never abort tuning on this)
    try:
        import backfill_history
        backfill_history.main()
    except Exception as exc:  # noqa: BLE001
        print(f"backfill skipped: {exc}", flush=True)

    # imported after backfill so STRATEGY_SETUPS is already set for classify_setup
    from learn_params import load_sessions, run_combo

    con = open_research_db("market")
    sessions = load_sessions(con)
    print(f"tuning over {len(sessions)} gapper sessions", flush=True)
    if len(sessions) < 10:
        print("too few sessions — keeping current learned_params.json")
        return

    grid = {
        "warmup": [3, 6],
        "min_bars": [3, 5],
        "eval_every": [1],
        "ready": [55, 60, 65, 70],
        "gap_min": [0.05],
        "rvol_min": [2.0],
    }
    keys = list(grid)
    best = None
    for combo in itertools.product(*grid.values()):
        p = dict(zip(keys, combo))
        if p["warmup"] < p["min_bars"]:
            continue
        m = run_combo(sessions, **p)
        if m["trades"] < 5 or m["pnl"] <= 0:
            continue
        # score: profit, lightly favouring earlier entries on ties
        score = m["pnl"] - (m["entry_min"] * 50.0)
        if best is None or score > best[2]:
            best = (p, m, score)

    if best is None:
        print("no profitable ORB config on this sample — keeping current params")
        return

    p, m, _ = best
    out = {
        "min_bars": p["min_bars"],
        "ready_score_pct": float(p["ready"]),
        "setups": "opening_range_break",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "sessions": len(sessions),
        "pnl": round(m["pnl"]),
        "win": round(m["win"], 3),
        "avg_R": round(m["avgR"], 3),
        "entry_min": round(m["entry_min"], 1),
    }
    path = os.path.join(os.environ.get("DATA_DIR", "./data"), "learned_params.json")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"wrote {path}:\n{json.dumps(out, indent=2)}", flush=True)


if __name__ == "__main__":
    main()
