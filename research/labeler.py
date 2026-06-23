"""Offline self-labeling + feature store over historical minute_bars.

Turns each historical symbol-session into a (features -> outcome) row so any
forecasting signal's LIFT can be measured BEFORE it's allowed to size live.

Two guarantees that make it trustworthy:
  1. STRICT TIME SPLIT — features use only bars at/before the decision point;
     labels use only bars strictly after it. No look-ahead.
  2. REUSE THE LIVE FUNCTIONS — opening_range / compute_key_levels /
     calculate_time_of_day_rvol / data-quality are the SAME code the live bot
     runs, so a labeled feature == what the bot sees (no train/serve skew).

Decision point (v1): opening-range completion (first ORB_BARS regular-hours
bars) — exactly the live ORB moment. entry_reference = ORB high, invalidation =
ORB low, so R = high-low and the R-outcomes are the live trade's outcomes.

Forward-only features (float, short-interest, real halts) are NOT in history;
they are left NULL and collected forward via shadow-mode — never backfilled.

CLI:
    python -m research.labeler build [--limit N] [--rebuild]
    python -m research.labeler report
"""

from __future__ import annotations

import argparse
import json

import pandas as pd

from research.multi_schema import open_research_db
from strategy.evaluation.data_quality import calculate_data_quality_score
from strategy.evaluation.levels import calculate_vwap, compute_key_levels
from strategy.evaluation.structure import opening_range
from strategy.evaluation.volume_metrics import calculate_time_of_day_rvol

FEATURE_VERSION = "v1"
LABEL_VERSION = "v1"
SETUP_VERSION = "v1"
ORB_BARS = 5
TARGET_R = 2.0

_MB_COLS = ["timestamp", "session_date", "is_premarket", "is_regular_hours",
           "is_afterhours", "open", "high", "low", "close", "volume", "vwap"]


def _preload_daily(con) -> dict:
    """{(symbol, date): (previous_close, avg_vol_20d)} — gap% + RVOL baselines."""
    out: dict = {}
    for sym, d, pc, av in con.execute(
        "SELECT symbol, trade_date, previous_close, rolling_avg_volume_20d FROM daily_bars"
    ).fetchall():
        out[(sym, d)] = (float(pc) if pc is not None else None,
                         float(av) if av else None)
    return out


def _preload_catalysts(con) -> dict:
    """{symbol: [(enriched_at, catalyst_type), ...]} sorted ascending."""
    out: dict = {}
    for sym, ts, ctype in con.execute(
        "SELECT symbol, enriched_at, catalyst_type FROM news_catalyst_cache "
        "WHERE catalyst_type IS NOT NULL ORDER BY enriched_at"
    ).fetchall():
        out.setdefault(sym, []).append((ts, ctype))
    return out


def _catalyst_at(cats: dict, symbol: str, setup_ts) -> tuple:
    """(catalyst_type, freshness_minutes) for the most recent classification
    AT/BEFORE setup_ts — no look-ahead into news that lands later."""
    rows = cats.get(symbol)
    if not rows:
        return (None, None)
    best = None
    for ts, ctype in rows:
        if ts is not None and ts <= setup_ts:
            best = (ts, ctype)
        else:
            break  # sorted ascending
    if best is None:
        return (None, None)
    fresh = (setup_ts - best[0]).total_seconds() / 60.0
    return (best[1], round(fresh, 1))


def build_one(con, symbol, session_date, daily: dict, cats: dict):
    """Fetch one symbol-session's bars + baselines, then compute. None if empty."""
    rows = con.execute(
        f"SELECT {', '.join(_MB_COLS)} FROM minute_bars "
        "WHERE symbol=? AND session_date=? ORDER BY timestamp",
        [symbol, session_date]).fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=_MB_COLS)
    prior_close, avg_vol = daily.get((symbol, session_date), (None, None))
    return compute_setup(symbol, session_date, df, prior_close, avg_vol, cats)


def compute_setup(symbol, session_date, df, prior_close, avg_vol, cats: dict):
    """PURE (no DB): (setup, features, labels) for one session's bars, or None.
    Strict time split — features use bars at/before the decision point, labels
    only after. Testable on synthetic bars."""
    df = df.copy()
    df["is_regular_hours"] = df["is_regular_hours"].astype(bool)
    df["is_premarket"] = df["is_premarket"].astype(bool)
    rth = df[df["is_regular_hours"]].reset_index(drop=True)
    if len(rth) < ORB_BARS:
        return None
    hi, lo, complete = opening_range(df, orb_bars=ORB_BARS)
    if not complete or hi is None or lo is None or hi <= lo:
        return None
    setup_ts = rth.iloc[ORB_BARS - 1]["timestamp"]
    session_open = float(rth.iloc[0]["open"])
    entry_ref, invalidation = float(hi), float(lo)
    R = entry_ref - invalidation
    if session_open <= 0 or R <= 0:
        return None

    # ---- FEATURES: only bars at/before the decision point ----------------
    upto = df[df["timestamp"] <= setup_ts].reset_index(drop=True)
    levels = compute_key_levels(upto, previous_close=prior_close,
                                opening_range_minutes=ORB_BARS)
    vwap = levels.vwap
    last_close = float(upto.iloc[-1]["close"])
    gap_pct = ((session_open - prior_close) / prior_close) if prior_close else None
    pm = df[df["is_premarket"]]
    pm_gap = (((float(pm["high"].max())) - prior_close) / prior_close) \
        if (prior_close and not pm.empty) else None
    vol_upto = float(upto["volume"].sum())
    minutes_elapsed = max(1, int((df["is_regular_hours"] & (df["timestamp"] <= setup_ts)).sum()))
    rvol = (vol_upto / avg_vol) if avg_vol else None
    tod_rvol = calculate_time_of_day_rvol(vol_upto, avg_vol, minutes_elapsed) if avg_vol else None
    dq = calculate_data_quality_score(upto)
    dist_vwap = ((last_close - vwap) / vwap) if vwap else None
    cat_type, cat_fresh = _catalyst_at(cats, symbol, setup_ts)
    above_vwap = bool(vwap is not None and last_close >= vwap)

    # ---- LABELS: only bars strictly AFTER the decision point --------------
    fwd = rth[rth["timestamp"] > setup_ts].reset_index(drop=True)

    def _win(n):
        return fwd[fwd["timestamp"] <= setup_ts + pd.Timedelta(minutes=n)]

    def _max_up(n):
        w = _win(n)
        return round((float(w["high"].max()) - entry_ref) / entry_ref, 5) if len(w) else None

    def _max_dd(n):
        w = _win(n)
        return round((float(w["low"].min()) - entry_ref) / entry_ref, 5) if len(w) else None

    # R-outcome — pessimistic: a bar that touches BOTH counts as the stop.
    reached_1r = reached_2r = False
    for _, b in fwd.iterrows():
        if float(b["low"]) <= invalidation:
            break
        if float(b["high"]) >= entry_ref + 2 * R:
            reached_2r = reached_1r = True
        elif float(b["high"]) >= entry_ref + R:
            reached_1r = True

    # held-VWAP over the EVOLVING cumulative session VWAP (not the per-bar vwap)
    rth2 = rth.copy().reset_index(drop=True)
    rth2["cvwap"] = calculate_vwap(rth2).values
    fwd_idx = rth2[rth2["timestamp"] > setup_ts]

    def _held_vwap(n):
        w = fwd_idx[fwd_idx["timestamp"] <= setup_ts + pd.Timedelta(minutes=n)]
        return bool(len(w) and (w["close"].astype(float) >= w["cvwap"]).all())

    failed = bool((fwd["high"].astype(float) > entry_ref).any()
                  and (fwd["low"].astype(float) < invalidation).any())
    session_high = float(rth["high"].max())
    high_vs_open = (session_high - session_open) / session_open
    trend_day = bool(high_vs_open >= 0.20)

    # time-to-max over the forward session
    t_up = t_dd = None
    if len(fwd):
        hi_i = fwd["high"].astype(float).idxmax()
        lo_i = fwd["low"].astype(float).idxmin()
        t_up = (fwd.iloc[hi_i]["timestamp"] - setup_ts).total_seconds() / 60.0
        t_dd = (fwd.iloc[lo_i]["timestamp"] - setup_ts).total_seconds() / 60.0

    sid = f"{symbol}:{session_date}:{SETUP_VERSION}"
    setup = dict(setup_id=sid, symbol=symbol, setup_time=setup_ts, session_date=session_date,
                 setup_name="orb_completion", setup_version=SETUP_VERSION,
                 entry_reference_price=entry_ref, invalidation_price=invalidation,
                 target_r_multiple=TARGET_R, above_vwap_flag=above_vwap,
                 vwap_at_trigger=vwap, gap_pct=gap_pct, relative_volume=rvol,
                 catalyst_freshness_at_trigger=cat_fresh, session_minute_number=minutes_elapsed)
    feat = dict(id=sid, timestamp=setup_ts, symbol=symbol, session_date=session_date,
                feature_version=FEATURE_VERSION, gap_pct=gap_pct, premarket_gap_pct=pm_gap,
                relative_volume=rvol, time_of_day_adjusted_relative_volume=tod_rvol,
                float_rotation_pct=None, vwap=vwap, distance_from_vwap=dist_vwap,
                ema9=levels.ema_9, ema20=levels.ema_20, catalyst_freshness_minutes=cat_fresh,
                catalyst_type=cat_type, tape_regime=None, spy_intraday_return=None,
                vix_level=None,
                metadata_json=json.dumps({"dq_score": round(dq.score, 3), "dq_grade": dq.grade,
                                          "bar_count_at_setup": len(upto), "R": round(R, 5)}))
    label = dict(setup_id=sid, label_version=LABEL_VERSION,
                 max_upside_next_5m=_max_up(5), max_upside_next_15m=_max_up(15),
                 max_upside_next_60m=_max_up(60), max_drawdown_next_5m=_max_dd(5),
                 max_drawdown_next_15m=_max_dd(15), max_drawdown_next_60m=_max_dd(60),
                 reached_1r_before_minus_1r=reached_1r, reached_2r_before_minus_1r=reached_2r,
                 held_vwap_5m=_held_vwap(5), held_vwap_15m=_held_vwap(15),
                 trend_day_flag=trend_day, failed_breakout_flag=failed,
                 time_to_max_upside_minutes=t_up, time_to_max_drawdown_minutes=t_dd)
    return setup, feat, label


def _insert(con, table, row):
    cols = list(row.keys())
    con.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES "
                f"({', '.join(['?'] * len(cols))})", [row[c] for c in cols])


def build(con, limit=None, rebuild=False):
    if rebuild:
        con.execute("DELETE FROM engineered_features WHERE feature_version=?", [FEATURE_VERSION])
        con.execute("DELETE FROM outcome_labels WHERE label_version=?", [LABEL_VERSION])
        con.execute("DELETE FROM setup_events WHERE setup_version=?", [SETUP_VERSION])
    daily = _preload_daily(con)
    cats = _preload_catalysts(con)
    sessions = con.execute(
        "SELECT DISTINCT symbol, session_date FROM minute_bars ORDER BY session_date, symbol"
        + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    built = skipped = 0
    for sym, d in sessions:
        try:
            res = build_one(con, sym, d, daily, cats)
        except Exception as exc:  # noqa: BLE001 — one bad session can't stop the build
            print(f"  ! {sym} {d}: {exc}")
            res = None
        if res is None:
            skipped += 1
            continue
        setup, feat, label = res
        _insert(con, "setup_events", setup)
        _insert(con, "engineered_features", feat)
        _insert(con, "outcome_labels", label)
        built += 1
        if built % 250 == 0:
            print(f"  ... {built} setups built ({skipped} sessions skipped)")
    print(f"\nbuilt {built} setups across {len(sessions)} symbol-sessions "
          f"({skipped} skipped: <{ORB_BARS} RTH bars or no range)")
    return built


def report(con):
    n = con.execute("SELECT count(*) FROM setup_events WHERE setup_version=?", [SETUP_VERSION]).fetchone()[0]
    if not n:
        print("No setups — run `build` first.")
        return
    days = con.execute("SELECT count(DISTINCT session_date) FROM setup_events WHERE setup_version=?", [SETUP_VERSION]).fetchone()[0]
    print(f"=== labeler report ({n} setups across {days} session-days) ===")
    # base rates — the continuation/runner question
    print("\nBASE RATES (forward, from the ORB decision point):")
    row = con.execute(
        "SELECT "
        "avg(CASE WHEN reached_1r_before_minus_1r THEN 1.0 ELSE 0 END), "
        "avg(CASE WHEN reached_2r_before_minus_1r THEN 1.0 ELSE 0 END), "
        "avg(CASE WHEN trend_day_flag THEN 1.0 ELSE 0 END), "
        "avg(CASE WHEN failed_breakout_flag THEN 1.0 ELSE 0 END), "
        "avg(max_upside_next_60m) "
        "FROM outcome_labels WHERE label_version=?", [LABEL_VERSION]).fetchone()
    print(f"  reached +1R before -1R : {row[0]*100:.0f}%")
    print(f"  reached +2R before -1R : {row[1]*100:.0f}%")
    print(f"  trend day (HoD>=+20% vs open): {row[2]*100:.0f}%")
    print(f"  failed breakout        : {row[3]*100:.0f}%")
    print(f"  avg max-upside in 60m  : {(row[4] or 0)*100:.1f}%")
    # the runner label, derived EXACTLY from minute_bars (no leakage; session
    # intraday-high vs the RTH open) — ONE aggregate query, not a per-setup loop.
    print("\nRUNNER BASE RATES (session intraday-high vs RTH open, derived from bars):")
    row = con.execute(
        "WITH se AS (SELECT DISTINCT symbol, session_date FROM setup_events WHERE setup_version=?), "
        "o AS (SELECT DISTINCT ON (m.symbol, m.session_date) m.symbol, m.session_date, m.open AS op "
        "      FROM minute_bars m JOIN se USING (symbol, session_date) "
        "      WHERE m.is_regular_hours ORDER BY m.symbol, m.session_date, m.timestamp), "
        "h AS (SELECT m.symbol, m.session_date, max(m.high) AS hod "
        "      FROM minute_bars m JOIN se USING (symbol, session_date) "
        "      WHERE m.is_regular_hours GROUP BY m.symbol, m.session_date), "
        "r AS (SELECT (h.hod - o.op)/o.op AS run FROM o JOIN h USING (symbol, session_date) WHERE o.op > 0) "
        "SELECT count(*), "
        "  sum(CASE WHEN run >= 0.20 THEN 1 ELSE 0 END), "
        "  sum(CASE WHEN run >= 0.50 THEN 1 ELSE 0 END), "
        "  sum(CASE WHEN run >= 1.00 THEN 1 ELSE 0 END) FROM r", [SETUP_VERSION]).fetchone()
    tot = int(row[0] or 0)
    if tot:
        r20, r50, r100 = int(row[1] or 0), int(row[2] or 0), int(row[3] or 0)
        print(f"  ran >= +20% : {r20}/{tot} = {r20/tot*100:.1f}%")
        print(f"  ran >= +50% : {r50}/{tot} = {r50/tot*100:.1f}%")
        print(f"  ran >=+100% : {r100}/{tot} = {r100/tot*100:.1f}%  (NXTS-class)")


def main():
    ap = argparse.ArgumentParser(description="offline labeler / feature store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--limit", type=int); b.add_argument("--rebuild", action="store_true")
    sub.add_parser("report")
    args = ap.parse_args()
    con = open_research_db("market")
    if args.cmd == "build":
        build(con, limit=args.limit, rebuild=args.rebuild)
    elif args.cmd == "report":
        report(con)


if __name__ == "__main__":
    main()
