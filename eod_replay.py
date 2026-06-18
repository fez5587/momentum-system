#!/usr/bin/env python3
"""End-of-day whole-market replay.

After the close, pull the day's full active universe (not just the ~20 the live
loop watched), backfill their bars, and run TODAY'S logic (entry filters + ORB +
managed exits) over every qualifying gapper — so you see what the system WOULD
have traded and the P&L, plus a perfect-hindsight line (the biggest catchable
moves) to show what was left on the table. Every run also GROWS the stored
dataset, so the nightly tuner and every verification get more trustworthy.

    PYTHONPATH=. python eod_replay.py            # replay today
    PYTHONPATH=. python eod_replay.py 2026-06-17 # replay a specific date

Schedule after the close (after eod flatten), e.g. cron: 5 16 * * 1-5.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

from dotenv import load_dotenv

sys.path.insert(0, ".")
load_dotenv()
os.environ.setdefault("STRATEGY_SETUPS", "opening_range_break")

from alpaca_paper.client import AlpacaPaperClient
from alpaca_paper.settings import AlpacaPaperSettings
from config import Config
from research.ingestion.market_data import (
    classify_session,
    ingest_daily_history,
    ingest_live_minute_bars,
)
from research.ingestion.signals import scan_gappers
from research.multi_schema import open_research_db
from research.query import query_avg_daily_volume, query_minute_bars, query_previous_close
from strategy.backtest.engine import BacktestEngine
from strategy.exits import ExitConfig, TRAIL_PRIOR_LOW, parse_profit_tiers
from strategy.models import SetupCriteria

# the live verified config
PRICE_MIN = float(os.environ.get("WATCHER_PRICE_MIN", "1"))
PRICE_MAX = float(os.environ.get("WATCHER_PRICE_MAX", "5"))
GAP_MIN = float(os.environ.get("TRIGGER_GAP_MIN", "3"))
GAP_MAX = float(os.environ.get("TRIGGER_GAP_MAX", "60"))
RVOL_MIN = float(os.environ.get("TRIGGER_RVOL_MIN", "2"))
RISK_PCT = float(os.environ.get("TRADING_RISK_PER_TRADE_PCT", "0.01"))


def _daily_gappers(con, sess, universe):
    """Identify today's qualifying gappers from DAILY bars (cheap) so we only
    minute-backfill the handful that matter, not all 100 names."""
    out = []
    for sym in universe:
        # Bars ON OR BEFORE the session date, newest first — so rows[0] is the
        # session day and rows[1] the prior trading day. Without the `<= sess`
        # bound this pulled the LATEST bar (always the most recent date in the
        # table), so replaying any past date saw rows[0] != sess and skipped
        # EVERY symbol — silently reporting "no qualifying gappers" for all
        # history and making multi-day backtesting impossible.
        rows = con.execute(
            "SELECT trade_date, open, close, volume FROM daily_bars WHERE symbol=? "
            "AND trade_date <= ? ORDER BY trade_date DESC LIMIT 25", [sym, sess]).fetchall()
        if len(rows) < 2 or str(rows[0][0]) != str(sess):
            continue
        t_open, t_close, t_vol = float(rows[0][1]), float(rows[0][2]), float(rows[0][3])
        prev_close = float(rows[1][2])
        prior = [float(r[3]) for r in rows[1:21]]
        adv = sum(prior) / len(prior) if prior else 0.0
        gap = (t_open / prev_close - 1) * 100 if prev_close else 0.0
        rvol = (t_vol / adv) if adv else 0.0
        if PRICE_MIN <= t_close <= PRICE_MAX and GAP_MIN <= gap <= GAP_MAX and rvol >= RVOL_MIN:
            out.append(sym)
    return out


def _exit_cfg() -> ExitConfig:
    return ExitConfig(
        target_r=float(os.environ.get("TRADING_REWARD_MULTIPLE", "10")),
        trail_mode=os.environ.get("TRADING_EXIT_TRAIL_MODE", TRAIL_PRIOR_LOW),
        trail_after_r=float(os.environ.get("TRADING_EXIT_TRAIL_AFTER_R", "1")),
        profit_lock_tiers=parse_profit_tiers(os.environ.get("TRADING_EXIT_PROFIT_TIERS", "")))


def main(argv=None):
    argv = argv or sys.argv[1:]
    if argv:
        sess = date.fromisoformat(argv[0])
    else:
        sess, _, _, _ = classify_session(datetime.now(timezone.utc))

    con = open_research_db("market")
    client = AlpacaPaperClient(AlpacaPaperSettings.from_env())

    # 1) full active universe (vs the ~20 the live loop watched)
    actives = client.get_most_actives(top=100, by="volume")
    universe = sorted({a["symbol"] for a in actives if a.get("symbol")})
    print(f"=== EOD REPLAY {sess} ===")
    print(f"universe: {len(universe)} most-active names (live loop watches ~20)")

    # 2) cheap DAILY backfill -> screen gappers from daily bars (no need to pull
    #    minute bars for all 100; only the gappers need them)
    ingest_daily_history(con, client, universe, days=30)
    gapper_syms = _daily_gappers(con, sess, universe)
    print(f"daily-screen gappers (${PRICE_MIN:g}-{PRICE_MAX:g}, gap {GAP_MIN:g}-{GAP_MAX:g}%, "
          f"rvol>={RVOL_MIN:g}): {len(gapper_syms)}")

    # 3) minute-backfill ONLY the gappers (fast), then rank them, and grow the dataset
    ing = ingest_live_minute_bars(con, client, gapper_syms, lookback_minutes=440)
    print(f"backfilled {ing.minute_rows} minute rows across {len(ing.symbols)} gappers\n")
    keep = set(gapper_syms)
    gappers = [g for g in scan_gappers(con, sess, min_gap_pct=GAP_MIN, min_relative_volume=RVOL_MIN,
                                       price_min=PRICE_MIN, price_max=PRICE_MAX, limit=200)
               if g.gap_pct <= GAP_MAX and g.symbol in keep]
    if not gappers:
        print("no qualifying gappers today.")
        return

    # 4) run TODAY'S logic over each
    cfg = _exit_cfg()
    eng = BacktestEngine(Config(), target_r=cfg.target_r, warmup_bars=3, eval_every=1,
                         ready_score_pct=55, min_bars=3, exit_config=cfg,
                         criteria=SetupCriteria(gap_pct_min=0.05, relative_volume_min=RVOL_MIN))
    equity = float(client.get_account().get("equity") or 0) or 100000.0
    risk_dollars = equity * RISK_PCT

    trades, hindsight = [], []
    for g in gappers:
        bars = query_minute_bars(con, g.symbol, sess)
        if bars is None or not len(bars):
            continue
        rth = bars[bars["is_regular_hours"] == True].reset_index(drop=True) if "is_regular_hours" in bars.columns else bars  # noqa: E712
        if len(rth):
            run_pct = (float(rth["high"].max()) / float(rth["open"].iloc[0]) - 1) * 100
            hindsight.append((g.symbol, g.gap_pct, run_pct))
        pc = query_previous_close(con, g.symbol, sess)
        adv = query_avg_daily_volume(con, g.symbol, sess)
        for t in eng.run(bars, g.symbol, previous_close=pc, avg_daily_volume=adv).trades:
            trades.append((g.symbol, t))

    # 5) report
    print("--- what TODAY'S logic would have traded ---")
    if trades:
        rs = [t.r_multiple or 0 for _s, t in trades]
        wins = sum(1 for r in rs if r > 0)
        sumR = sum(rs)
        print(f"  trades: {len(trades)}   win: {wins / len(trades) * 100:.0f}%   "
              f"total R: {sumR:+.1f}   est P&L @ ${equity:,.0f} (1% risk): ${sumR * risk_dollars:+,.0f}")
        for sym, t in sorted(trades, key=lambda x: -(x[1].r_multiple or 0))[:8]:
            print(f"    {sym:6} {t.r_multiple:+.2f}R  entry {t.entry_price} -> "
                  f"{t.exit_reason} {t.exit_price}")
    else:
        print("  no trades taken.")

    print("\n--- perfect hindsight (biggest intraday runs from the open) ---")
    for sym, gap, run_pct in sorted(hindsight, key=lambda x: -x[2])[:8]:
        print(f"    {sym:6} gap {gap:+.0f}%  ran +{run_pct:.0f}% intraday")
    captured = sum(r for r in [t.r_multiple or 0 for _s, t in trades]) if trades else 0
    print(f"\n  gappers available: {len(gappers)}  |  traded: {len(set(s for s, _ in trades))}  "
          f"|  captured {captured:+.1f}R of the day's opportunity")


if __name__ == "__main__":
    main()
