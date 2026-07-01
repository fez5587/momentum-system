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
from strategy.evaluation.runner import detect_leading_gainer_runner
from strategy.evaluation.runner_propensity import runner_propensity
from strategy.evaluation.volume_metrics import calculate_time_of_day_rvol
from strategy.evaluation.vwap_pullback_entry import find_vwap_pullback_entry
from strategy.evaluation.vwap_reclaim import detect_vwap_reclaim
from strategy.exits import ExitConfig, simulate_exit

FEATURE_VERSION = "v1"
LABEL_VERSION = "v1"
SETUP_VERSION = "v1"
ORB_BARS = 5
TARGET_R = 2.0

# vwap_reclaim shadow track (P2): the trader's first-pullback continuation, scored by
# the SAME forward machinery as the ORB labeler so its base rates are comparable.
VR_SETUP_VERSION = "vr1"
VR_NAME = "vwap_reclaim"
VR_MIN_BARS = 21

# P3 PRE-REGISTERED promotion hypothesis (locked 2026-06-29 from the edit-audit, BEFORE
# any forward data). The raw setup is negative after cost (stop-unit artifact); the only
# subset that survived adversarial refutation is price>=$5 AND impulse in the MID tercile,
# scored as a +2R/RUNNER filter (NOT +1R). These bands are POST-HOC on the 24-day window
# -- a HYPOTHESIS to validate forward, never to be hand-tuned to fit. PASS BAR: on
# out-of-sample fires the conditioned subset's +2R Wilson-95 lower bound must exceed the
# base +2R rate AND its absolute max-upside-60m must beat same-symbol-day ORB at +5%/+10%.
VR_PROMO_MIN_PRICE = 5.0
VR_PROMO_IMPULSE_LO = 0.15
VR_PROMO_IMPULSE_HI = 0.26
VR_PROMO_BASE_2R = 0.349        # the unconditioned +2R base rate the subset must clear

# leading-gainer runner shadow track (run1): slide detect_leading_gainer_runner over
# history (PM-INCLUSIVE — the run that matters starts pre-market), emit a non-tradeable
# setup on each fresh fire, score with the SAME forward machinery. The detector separates
# the runner LABEL cleanly (2026-06-30 in-sample: 5/5 runners fire, 7/7 chop silent) but
# the OPEN QUESTION is conversion: SVRE/JEM put ~100% of the move in pre-market, so an RTH
# fire is late (+242%/+346% already run). This track measures whether that late entry pays.
RUNNER_SETUP_VERSION = "run1"
RUNNER_NAME = "leading_gainer_runner"
RUNNER_MIN_BARS = 14            # detector history need (velocity/struct/burst windows)
RUNNER_MIN_RTH_BARS = 20       # forward room so the outcome labels aren't truncated

# PRE-REGISTERED promotion hypothesis (locked 2026-06-30, BEFORE any forward data). The
# detector confirms AT/AFTER the top for PM-driven names, so the null is "separates the
# label, negative through the bracket" — the same trap 4 prior selection levers hit. To
# promote off shadow-only, out-of-sample fires must clear ALL of:
RUNNER_PROMO_MEDIAN_FWDMAX_60M = 0.15   # (1) median max-upside 60m >= +15% (real continuation)
RUNNER_PROMO_MEDIAN_ADVERSE_60M = -0.08  # (2) median max-drawdown 60m > -8% (survivable stop)
RUNNER_PROMO_MIN_1R = 0.50               # (3) +1R-before-(-1R) > 50% (positive 1:1 expectancy)
RUNNER_PM_EXHAUSTION_FRAC = 0.70         # PM captured > this fraction of the run => "spent"
# (4) beat vwap_reclaim +1R on the SAME symbol-days, and (5) dropping PM-exhausted fires
#     must RAISE the +1R rate (else the exhaustion tag earns nothing) — computed in validate.

# No-chase entry shadow-validation (the final mechanic lever). Realistic per-name round-trip
# cost proxy (minute_bars has no NBBO, so a price-tiered spread/slippage stand-in). PRE-
# REGISTERED bar locked BEFORE forward data: promote the no-chase VWAP-pullback entry only if
# its mean PERCENT return (net of this cost) is STRICTLY POSITIVE out-of-sample.
NOCHASE_COST_SUB3 = 0.04        # sub-$3 microcaps (~55% of the gapper book) — wide spreads
NOCHASE_COST_OTHER = 0.02       # >=$3 names


def _round_trip_cost(price: float) -> float:
    return NOCHASE_COST_SUB3 if price < 3.0 else NOCHASE_COST_OTHER


def _wilson_lower(k: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for k/n at confidence z (default 95%).
    Honest small-sample lower bound -- the bar a rate must clear, not the point estimate."""
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return max(0.0, (centre - half) / d)

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


def _forward_labels(rth, setup_ts, entry_ref, invalidation, session_open) -> dict:
    """Forward-only outcome labels from a decision point. STRICT time split: uses
    only RTH bars strictly AFTER setup_ts; entry_ref/invalidation define R. Shared by
    the ORB labeler and the vwap_reclaim shadow track so both are scored identically
    (no skew). Pessimistic R-outcome: a bar that touches BOTH +R and -1R counts as the
    stop. held-VWAP uses the EVOLVING cumulative session VWAP, not the per-bar vwap."""
    R = entry_ref - invalidation
    fwd = rth[rth["timestamp"] > setup_ts].reset_index(drop=True)

    def _win(n):
        return fwd[fwd["timestamp"] <= setup_ts + pd.Timedelta(minutes=n)]

    def _max_up(n):
        w = _win(n)
        return round((float(w["high"].max()) - entry_ref) / entry_ref, 5) if len(w) else None

    def _max_dd(n):
        w = _win(n)
        return round((float(w["low"].min()) - entry_ref) / entry_ref, 5) if len(w) else None

    reached_1r = reached_2r = False
    for _, b in fwd.iterrows():
        if float(b["low"]) <= invalidation:
            break
        if float(b["high"]) >= entry_ref + 2 * R:
            reached_2r = reached_1r = True
        elif float(b["high"]) >= entry_ref + R:
            reached_1r = True

    rth2 = rth.copy().reset_index(drop=True)
    rth2["cvwap"] = calculate_vwap(rth2).values
    fwd_idx = rth2[rth2["timestamp"] > setup_ts]

    def _held_vwap(n):
        w = fwd_idx[fwd_idx["timestamp"] <= setup_ts + pd.Timedelta(minutes=n)]
        return bool(len(w) and (w["close"].astype(float) >= w["cvwap"]).all())

    failed = bool((fwd["high"].astype(float) > entry_ref).any()
                  and (fwd["low"].astype(float) < invalidation).any())
    session_high = float(rth["high"].max())
    high_vs_open = (session_high - session_open) / session_open if session_open else 0.0
    trend_day = bool(high_vs_open >= 0.20)

    t_up = t_dd = None
    if len(fwd):
        hi_i = fwd["high"].astype(float).idxmax()
        lo_i = fwd["low"].astype(float).idxmin()
        t_up = (fwd.iloc[hi_i]["timestamp"] - setup_ts).total_seconds() / 60.0
        t_dd = (fwd.iloc[lo_i]["timestamp"] - setup_ts).total_seconds() / 60.0

    return dict(max_upside_next_5m=_max_up(5), max_upside_next_15m=_max_up(15),
                max_upside_next_60m=_max_up(60), max_drawdown_next_5m=_max_dd(5),
                max_drawdown_next_15m=_max_dd(15), max_drawdown_next_60m=_max_dd(60),
                reached_1r_before_minus_1r=reached_1r, reached_2r_before_minus_1r=reached_2r,
                held_vwap_5m=_held_vwap(5), held_vwap_15m=_held_vwap(15),
                trend_day_flag=trend_day, failed_breakout_flag=failed,
                time_to_max_upside_minutes=t_up, time_to_max_drawdown_minutes=t_dd)


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

    # ---- LABELS: only bars strictly AFTER the decision point (shared helper) --
    lab = _forward_labels(rth, setup_ts, entry_ref, invalidation, session_open)

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
    label = dict(setup_id=sid, label_version=LABEL_VERSION, **lab)
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


# ----------------------------------------------------------------------------
# vwap_reclaim shadow track (P2): slide the live detector over history, emit a
# non-tradeable setup_event on each fire, score it with the SAME forward labels.
# ----------------------------------------------------------------------------

def compute_vwap_reclaim_setups(symbol, session_date, df, prior_close, avg_vol,
                                cats: dict, *, cooldown=10) -> list:
    """0..N shadow vwap_reclaim setups for one session. Slides detect_vwap_reclaim
    over the RTH bars and emits on each RISING EDGE (not-valid -> valid) with a bar
    cooldown so one curl episode is one setup. Each emit carries the detector's OWN
    entry/stop/R; labels come from _forward_labels (no skew). STRICT time split: the
    detector at bar i sees only bars[:i+1]; labels use only RTH bars after i. PURE.

    The shadow base track keeps EVERY curl fire (placeability gate OFF: min_r_*=0) so
    report-vr's base rates stay complete and the placeability filter's effect is
    measurable; validate-vr applies placeability + the promotion conditioning. The live
    detector defaults the gate ON -- that is a live-tradeability default, not a base-rate
    one."""
    df = df.copy()
    df["is_regular_hours"] = df["is_regular_hours"].astype(bool)
    rth = df[df["is_regular_hours"]].reset_index(drop=True)
    if len(rth) < VR_MIN_BARS:
        return []
    session_open = float(rth.iloc[0]["open"])
    if session_open <= 0:
        return []
    out, prev_fire, last_emit = [], False, -10 ** 9
    for i in range(VR_MIN_BARS - 1, len(rth)):
        sig = detect_vwap_reclaim(rth.iloc[:i + 1], min_r_frac=0.0, min_r_abs=0.0)
        firing = sig.is_valid
        rising = firing and not prev_fire and (i - last_emit) >= cooldown
        prev_fire = firing
        if not rising:
            continue
        entry_ref = float(sig.breakout_level)
        invalidation = float(sig.stop_level)
        if entry_ref <= invalidation:
            continue
        last_emit = i
        setup_ts = rth.iloc[i]["timestamp"]
        sv = sig.signal_values
        vwap_at = round(entry_ref - float(sv.get("dist_from_vwap") or 0.0), 5)
        _, cat_fresh = _catalyst_at(cats, symbol, setup_ts)
        # key on the RTH bar INDEX (unique per session) -- NOT the wall-clock minute:
        # some sessions have sub-minute/irregular timestamps, so two fires a cooldown
        # apart can share an HH:MM and collide on the primary key.
        sid = f"{symbol}:{session_date}:b{i}:{VR_SETUP_VERSION}"
        setup = dict(setup_id=sid, symbol=symbol, setup_time=setup_ts, session_date=session_date,
                     setup_name=VR_NAME, setup_version=VR_SETUP_VERSION,
                     entry_reference_price=entry_ref, invalidation_price=invalidation,
                     target_r_multiple=round(float(sv.get("target_rr") or 0.0), 2),
                     impulse_pct=sv.get("run_pct"), pullback_low=invalidation,
                     pullback_depth_pct=sv.get("pullback_held_frac"),
                     above_vwap_flag=True, vwap_at_trigger=vwap_at,
                     catalyst_freshness_at_trigger=cat_fresh, session_minute_number=i + 1)
        lab = _forward_labels(rth, setup_ts, entry_ref, invalidation, session_open)
        out.append((setup, dict(setup_id=sid, label_version=LABEL_VERSION, **lab)))
    return out


def build_vwap_reclaim(con, limit=None, rebuild=False, cooldown=10):
    if rebuild:
        con.execute("DELETE FROM outcome_labels WHERE setup_id IN "
                    "(SELECT setup_id FROM setup_events WHERE setup_version=?)", [VR_SETUP_VERSION])
        con.execute("DELETE FROM setup_events WHERE setup_version=?", [VR_SETUP_VERSION])
    daily = _preload_daily(con)
    cats = _preload_catalysts(con)
    sessions = con.execute(
        "SELECT DISTINCT symbol, session_date FROM minute_bars ORDER BY session_date, symbol"
        + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    built = sess_with = 0
    for sym, d in sessions:
        rows = con.execute(
            f"SELECT {', '.join(_MB_COLS)} FROM minute_bars WHERE symbol=? AND session_date=? "
            "ORDER BY timestamp", [sym, d]).fetchall()
        if not rows:
            continue
        dfb = pd.DataFrame(rows, columns=_MB_COLS)
        pc, av = daily.get((sym, d), (None, None))
        try:
            setups = compute_vwap_reclaim_setups(sym, d, dfb, pc, av, cats, cooldown=cooldown)
        except Exception as exc:  # noqa: BLE001 — one bad session can't stop the build
            print(f"  ! {sym} {d}: {exc}")
            continue
        if setups:
            sess_with += 1
        for setup, label in setups:
            _insert(con, "setup_events", setup)
            _insert(con, "outcome_labels", label)
            built += 1
    print(f"built {built} vwap_reclaim shadow fires across {sess_with} sessions "
          f"(of {len(sessions)} symbol-sessions scanned)")
    return built


def report_vwap_reclaim(con):
    n = con.execute("SELECT count(*) FROM setup_events WHERE setup_version=?", [VR_SETUP_VERSION]).fetchone()[0]
    if not n:
        print("No vwap_reclaim setups — run `build-vr` first.")
        return
    days, syms = con.execute(
        "SELECT count(DISTINCT session_date), count(DISTINCT symbol) FROM setup_events "
        "WHERE setup_version=?", [VR_SETUP_VERSION]).fetchone()
    print(f"=== vwap_reclaim shadow report ({n} fires, {syms} symbols, {days} session-days) ===")
    row = con.execute(
        "SELECT avg(CASE WHEN l.reached_1r_before_minus_1r THEN 1.0 ELSE 0 END), "
        "avg(CASE WHEN l.reached_2r_before_minus_1r THEN 1.0 ELSE 0 END), "
        "avg(CASE WHEN l.failed_breakout_flag THEN 1.0 ELSE 0 END), "
        "avg(CASE WHEN l.held_vwap_15m THEN 1.0 ELSE 0 END), "
        "avg(l.max_upside_next_15m), avg(l.max_upside_next_60m), avg(l.max_drawdown_next_15m), "
        "avg(se.target_r_multiple) "
        "FROM outcome_labels l JOIN setup_events se ON se.setup_id = l.setup_id "
        "WHERE se.setup_version=?", [VR_SETUP_VERSION]).fetchone()
    print("\nFORWARD OUTCOMES (from each curl-fire, strict time split):")
    print(f"  reached +1R before -1R : {row[0]*100:.0f}%")
    print(f"  reached +2R before -1R : {row[1]*100:.0f}%")
    print(f"  failed (poke then lose stop): {row[2]*100:.0f}%")
    print(f"  held VWAP 15m          : {row[3]*100:.0f}%")
    print(f"  avg max-upside 15m/60m : {(row[4] or 0)*100:.1f}% / {(row[5] or 0)*100:.1f}%")
    print(f"  avg max-drawdown 15m   : {(row[6] or 0)*100:.1f}%")
    print(f"  avg detector target R:R (to local high): {row[7] or 0:.1f}")
    # head-to-head vs the ORB baseline (v1) on the SAME symbol-days, if it's built
    cmp = con.execute(
        "WITH vr AS (SELECT DISTINCT symbol, session_date FROM setup_events WHERE setup_version=?) "
        "SELECT avg(CASE WHEN l.reached_1r_before_minus_1r THEN 1.0 ELSE 0 END), count(*) "
        "FROM setup_events se JOIN outcome_labels l ON l.setup_id = se.setup_id "
        "JOIN vr ON vr.symbol = se.symbol AND vr.session_date = se.session_date "
        "WHERE se.setup_version=?", [VR_SETUP_VERSION, SETUP_VERSION]).fetchone()
    if cmp and cmp[1]:
        print(f"\nHEAD-TO-HEAD (same symbol-days): ORB '{SETUP_VERSION}' reached +1R = "
              f"{(cmp[0] or 0)*100:.0f}% over {cmp[1]} ORB setups (vs the vwap_reclaim {row[0]*100:.0f}% above)")


def validate_vwap_reclaim(con, min_r_frac=0.015, min_r_abs=0.02):
    """Score the PRE-REGISTERED promotion candidate against the locked pass bar (P3).
    Candidate = vr1 fires that are PLACEABLE (R >= max(min_r_abs, min_r_frac*entry))
    AND price >= VR_PROMO_MIN_PRICE AND impulse in [VR_PROMO_IMPULSE_LO, _HI]. The bar:
      (1) +2R Wilson-95 lower bound > VR_PROMO_BASE_2R (a runner filter, not a +1R one), and
      (2) absolute max-upside-60m beats same-symbol-day ORB at BOTH +5% and +10%.
    Runs identically on in-sample and forward data; in-sample is a HYPOTHESIS, not a pass."""
    sub = con.execute(
        "SELECT se.symbol, se.session_date, "
        "CASE WHEN l.reached_2r_before_minus_1r THEN 1 ELSE 0 END, l.max_upside_next_60m "
        "FROM setup_events se JOIN outcome_labels l ON l.setup_id = se.setup_id "
        "WHERE se.setup_version=? AND se.entry_reference_price >= ? "
        "AND se.impulse_pct >= ? AND se.impulse_pct <= ? "
        "AND (se.entry_reference_price - se.invalidation_price) >= "
        "    GREATEST(?, ? * se.entry_reference_price)",
        [VR_SETUP_VERSION, VR_PROMO_MIN_PRICE, VR_PROMO_IMPULSE_LO, VR_PROMO_IMPULSE_HI,
         min_r_abs, min_r_frac]).fetchall()
    n = len(sub)
    print("=== vwap_reclaim PROMOTION-CANDIDATE validation (P3, pre-registered bar) ===")
    print(f"  rule: placeable (R>=max(${min_r_abs:.2f},{min_r_frac*100:.1f}%)) AND price>=${VR_PROMO_MIN_PRICE:.0f} "
          f"AND impulse in [{VR_PROMO_IMPULSE_LO},{VR_PROMO_IMPULSE_HI}]")
    if not n:
        print("  no candidate fires yet (run build-vr first / awaiting forward data).")
        return
    k2r = sum(r[2] for r in sub)
    lb = _wilson_lower(k2r, n)
    up5 = sum(1 for r in sub if (r[3] or 0) >= 0.05) / n
    up10 = sum(1 for r in sub if (r[3] or 0) >= 0.10) / n
    days = len({(r[0], r[1]) for r in sub})
    # ORB on the SAME symbol-days, absolute 60m up-move thresholds
    orb = con.execute(
        "WITH sd AS (SELECT DISTINCT se.symbol, se.session_date "
        "  FROM setup_events se JOIN outcome_labels l ON l.setup_id=se.setup_id "
        "  WHERE se.setup_version=? AND se.entry_reference_price >= ? "
        "  AND se.impulse_pct >= ? AND se.impulse_pct <= ? "
        "  AND (se.entry_reference_price - se.invalidation_price) >= GREATEST(?, ? * se.entry_reference_price)) "
        "SELECT avg(CASE WHEN l.max_upside_next_60m>=0.05 THEN 1.0 ELSE 0 END), "
        "       avg(CASE WHEN l.max_upside_next_60m>=0.10 THEN 1.0 ELSE 0 END), count(*) "
        "FROM setup_events se JOIN outcome_labels l ON l.setup_id=se.setup_id "
        "JOIN sd ON sd.symbol=se.symbol AND sd.session_date=se.session_date "
        "WHERE se.setup_version=?",
        [VR_SETUP_VERSION, VR_PROMO_MIN_PRICE, VR_PROMO_IMPULSE_LO, VR_PROMO_IMPULSE_HI,
         min_r_abs, min_r_frac, SETUP_VERSION]).fetchone()
    orb_up5, orb_up10, orb_n = (orb[0] or 0), (orb[1] or 0), int(orb[2] or 0)
    pass_2r = lb > VR_PROMO_BASE_2R
    pass_abs = (up5 >= orb_up5) and (up10 >= orb_up10)
    mark = lambda ok: "PASS" if ok else "FAIL"
    print(f"  candidate: {n} fires, {days} session-days")
    print(f"  +2R rate {k2r}/{n} = {k2r/n*100:.0f}% | Wilson-95 LB {lb*100:.1f}% vs base "
          f"{VR_PROMO_BASE_2R*100:.1f}%  -> [{mark(pass_2r)}]")
    print(f"  abs +5%/60m  {up5*100:.0f}% vs ORB {orb_up5*100:.0f}% | "
          f"+10%/60m {up10*100:.0f}% vs ORB {orb_up10*100:.0f}% (ORB n={orb_n})  -> [{mark(pass_abs)}]")
    print(f"  OVERALL: [{mark(pass_2r and pass_abs)}]")
    print("  NOTE: in-sample (post-hoc tercile selection) is a HYPOTHESIS, not a pass. "
          "The bar is locked; only OUT-OF-SAMPLE forward fires can clear it.")


# ----------------------------------------------------------------------------
# leading-gainer runner shadow track (run1): slide the detector over history,
# emit a non-tradeable setup on each fresh fire, score it with _forward_labels.
# ----------------------------------------------------------------------------

def compute_runner_setups(symbol, session_date, df, prior_close, avg_vol,
                          cats: dict, *, cooldown=10) -> list:
    """0..N shadow leading-gainer-runner setups for one session. Slides
    detect_leading_gainer_runner over the session (PM-INCLUSIVE: the detector at RTH
    bar k sees pm+rth[:k+1], so run%/VWAP are full-session), emits on each RISING EDGE
    (not-valid -> valid) with a bar cooldown so one leg is one setup. Only RTH bars can
    be decision points (a fire is only tradeable in RTH). Each emit carries the detector's
    OWN entry (last close) / stop (last higher low); labels come from _forward_labels over
    RTH-only bars strictly after the fire — identical scoring to the ORB and vwap_reclaim
    tracks (no skew). PURE. The PM-exhaustion measure (pm_capture) is stored in
    pullback_depth_pct so validate-runner can test whether skipping spent runners pays."""
    df = df.copy()
    df["is_regular_hours"] = df["is_regular_hours"].astype(bool)
    df["is_premarket"] = df["is_premarket"].astype(bool)
    full = df.reset_index(drop=True)
    rth = df[df["is_regular_hours"]].reset_index(drop=True)
    if len(rth) < RUNNER_MIN_RTH_BARS:
        return []
    session_open = float(rth.iloc[0]["open"])
    if session_open <= 0:
        return []
    pm = full[full["is_premarket"]]
    pm_high = float(pm["high"].max()) if len(pm) else None
    pc = float(prior_close) if prior_close else None
    ohlcv = ["open", "high", "low", "close", "volume"]
    # cumulative VWAP is a PREFIX function -> compute the whole-session array ONCE and hand
    # the detector vwap[k] per bar; identical to recomputing on each slice, but O(n) not O(n^2).
    vwap_full = calculate_vwap(full).astype(float).to_numpy()

    rth_positions = [k for k in range(len(full)) if bool(full.iloc[k]["is_regular_hours"])]
    out, prev_fire, last_emit = [], False, -10 ** 9
    for j, k in enumerate(rth_positions):
        if k + 1 < RUNNER_MIN_BARS:
            continue
        window = full.iloc[:k + 1][ohlcv].astype(float)
        sig = detect_leading_gainer_runner(window, day_base=pc, pm_high=pm_high,
                                           pm_exhaustion_frac=RUNNER_PM_EXHAUSTION_FRAC,
                                           session_vwap=float(vwap_full[k]))
        firing = sig.is_valid
        rising = firing and not prev_fire and (k - last_emit) >= cooldown
        prev_fire = firing
        if not rising:
            continue
        entry_ref = float(sig.entry_level)
        invalidation = float(sig.stop_level)
        if entry_ref <= invalidation:
            continue
        last_emit = k
        setup_ts = full.iloc[k]["timestamp"]
        sv = sig.signal_values
        # signal_values carry numpy scalars; psycopg2 can't adapt np.float64 -> cast native
        nf = lambda key: (float(sv[key]) if sv.get(key) is not None else None)
        _, cat_fresh = _catalyst_at(cats, symbol, setup_ts)
        sid = f"{symbol}:{session_date}:b{k}:{RUNNER_SETUP_VERSION}"
        setup = dict(setup_id=sid, symbol=symbol, setup_time=setup_ts, session_date=session_date,
                     setup_name=RUNNER_NAME, setup_version=RUNNER_SETUP_VERSION,
                     entry_reference_price=entry_ref, invalidation_price=invalidation,
                     target_r_multiple=0.0, impulse_pct=nf("velocity_pct"),
                     gap_pct=nf("total_run"), relative_volume=nf("volume_burst_ratio"),
                     above_vwap_flag=True, vwap_at_trigger=nf("session_vwap"),
                     pullback_depth_pct=nf("pm_capture"),
                     catalyst_freshness_at_trigger=cat_fresh, session_minute_number=j + 1)
        lab = _forward_labels(rth, setup_ts, entry_ref, invalidation, session_open)
        out.append((setup, dict(setup_id=sid, label_version=LABEL_VERSION, **lab)))
    return out


def build_runner(con, limit=None, rebuild=False, cooldown=10):
    if rebuild:
        con.execute("DELETE FROM outcome_labels WHERE setup_id IN "
                    "(SELECT setup_id FROM setup_events WHERE setup_version=?)", [RUNNER_SETUP_VERSION])
        con.execute("DELETE FROM setup_events WHERE setup_version=?", [RUNNER_SETUP_VERSION])
    daily = _preload_daily(con)
    cats = _preload_catalysts(con)
    sessions = con.execute(
        "SELECT DISTINCT symbol, session_date FROM minute_bars ORDER BY session_date, symbol"
        + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    built = sess_with = 0
    for sym, d in sessions:
        rows = con.execute(
            f"SELECT {', '.join(_MB_COLS)} FROM minute_bars WHERE symbol=? AND session_date=? "
            "ORDER BY timestamp", [sym, d]).fetchall()
        if not rows:
            continue
        dfb = pd.DataFrame(rows, columns=_MB_COLS)
        pc, av = daily.get((sym, d), (None, None))
        try:
            setups = compute_runner_setups(sym, d, dfb, pc, av, cats, cooldown=cooldown)
        except Exception as exc:  # noqa: BLE001 — one bad session can't stop the build
            print(f"  ! {sym} {d}: {exc}")
            continue
        if setups:
            sess_with += 1
        for setup, label in setups:
            _insert(con, "setup_events", setup)
            _insert(con, "outcome_labels", label)
            built += 1
    print(f"built {built} leading_gainer_runner shadow fires across {sess_with} sessions "
          f"(of {len(sessions)} symbol-sessions scanned)")
    return built


def report_runner(con):
    n = con.execute("SELECT count(*) FROM setup_events WHERE setup_version=?",
                    [RUNNER_SETUP_VERSION]).fetchone()[0]
    if not n:
        print("No leading_gainer_runner setups — run `build-runner` first.")
        return
    days, syms = con.execute(
        "SELECT count(DISTINCT session_date), count(DISTINCT symbol) FROM setup_events "
        "WHERE setup_version=?", [RUNNER_SETUP_VERSION]).fetchone()
    print(f"=== leading_gainer_runner shadow report ({n} fires, {syms} symbols, {days} session-days) ===")
    row = con.execute(
        "SELECT avg(CASE WHEN l.reached_1r_before_minus_1r THEN 1.0 ELSE 0 END), "
        "avg(CASE WHEN l.reached_2r_before_minus_1r THEN 1.0 ELSE 0 END), "
        "avg(CASE WHEN l.failed_breakout_flag THEN 1.0 ELSE 0 END), "
        "avg(CASE WHEN l.held_vwap_15m THEN 1.0 ELSE 0 END), "
        "avg(l.max_upside_next_15m), avg(l.max_upside_next_60m), avg(l.max_drawdown_next_60m), "
        "avg(se.gap_pct) "
        "FROM outcome_labels l JOIN setup_events se ON se.setup_id = l.setup_id "
        "WHERE se.setup_version=?", [RUNNER_SETUP_VERSION]).fetchone()
    print("\nFORWARD OUTCOMES (from each runner fire, strict RTH time split):")
    print(f"  reached +1R before -1R : {row[0]*100:.0f}%")
    print(f"  reached +2R before -1R : {row[1]*100:.0f}%")
    print(f"  failed (poke then lose stop): {row[2]*100:.0f}%")
    print(f"  held VWAP 15m          : {row[3]*100:.0f}%")
    print(f"  avg max-upside 15m/60m : {(row[4] or 0)*100:.1f}% / {(row[5] or 0)*100:.1f}%")
    print(f"  avg max-drawdown 60m   : {(row[6] or 0)*100:.1f}%")
    print(f"  avg session run at fire (gap_pct): {(row[7] or 0)*100:.0f}%")
    # PM-exhaustion split: pm_capture stored in pullback_depth_pct
    for label, cond in [("PM-exhausted (spent)", f"se.pullback_depth_pct > {RUNNER_PM_EXHAUSTION_FRAC}"),
                        ("fresh (not spent)",
                         f"(se.pullback_depth_pct IS NULL OR se.pullback_depth_pct <= {RUNNER_PM_EXHAUSTION_FRAC})")]:
        r = con.execute(
            "SELECT count(*), avg(CASE WHEN l.reached_1r_before_minus_1r THEN 1.0 ELSE 0 END) "
            "FROM outcome_labels l JOIN setup_events se ON se.setup_id = l.setup_id "
            f"WHERE se.setup_version=? AND {cond}", [RUNNER_SETUP_VERSION]).fetchone()
        if r and r[0]:
            print(f"  [{label:22}] n={r[0]:4}  +1R={( r[1] or 0)*100:.0f}%")
    cmp = con.execute(
        "WITH rn AS (SELECT DISTINCT symbol, session_date FROM setup_events WHERE setup_version=?) "
        "SELECT avg(CASE WHEN l.reached_1r_before_minus_1r THEN 1.0 ELSE 0 END), count(*) "
        "FROM setup_events se JOIN outcome_labels l ON l.setup_id = se.setup_id "
        "JOIN rn ON rn.symbol = se.symbol AND rn.session_date = se.session_date "
        "WHERE se.setup_version=?", [RUNNER_SETUP_VERSION, SETUP_VERSION]).fetchone()
    if cmp and cmp[1]:
        print(f"\nHEAD-TO-HEAD (same symbol-days): ORB '{SETUP_VERSION}' reached +1R = "
              f"{(cmp[0] or 0)*100:.0f}% over {cmp[1]} ORB setups (vs runner {row[0]*100:.0f}% above)")


def _median(xs) -> float | None:
    xs = sorted(v for v in xs if v is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def validate_runner(con):
    """Score the leading_gainer_runner shadow track against its PRE-REGISTERED bar.
    ALL five conditions must PASS out-of-sample to even consider promotion; the track is
    born under the null 'separates the label, negative through the bracket'."""
    n = con.execute("SELECT count(*) FROM setup_events WHERE setup_version=?",
                    [RUNNER_SETUP_VERSION]).fetchone()[0]
    print(f"=== validate leading_gainer_runner vs the locked bar (n={n}) ===")
    if not n:
        print("No fires — run `build-runner` first.")
        return
    rows = con.execute(
        "SELECT l.max_upside_next_60m, l.max_drawdown_next_60m, "
        "CASE WHEN l.reached_1r_before_minus_1r THEN 1 ELSE 0 END, se.pullback_depth_pct "
        "FROM outcome_labels l JOIN setup_events se ON se.setup_id = l.setup_id "
        "WHERE se.setup_version=?", [RUNNER_SETUP_VERSION]).fetchall()
    up = [r[0] for r in rows]
    dd = [r[1] for r in rows]
    r1 = [r[2] for r in rows]
    med_up = _median(up) or 0.0
    med_dd = _median(dd) or 0.0
    rate_1r = sum(r1) / len(r1)
    lb_1r = _wilson_lower(sum(r1), len(r1))
    fresh = [r[2] for r in rows if r[3] is None or r[3] <= RUNNER_PM_EXHAUSTION_FRAC]
    rate_fresh = (sum(fresh) / len(fresh)) if fresh else 0.0
    orb = con.execute(
        "WITH rn AS (SELECT DISTINCT symbol, session_date FROM setup_events WHERE setup_version=?) "
        "SELECT avg(CASE WHEN l.reached_1r_before_minus_1r THEN 1.0 ELSE 0 END), count(*) "
        "FROM setup_events se JOIN outcome_labels l ON l.setup_id = se.setup_id "
        "JOIN rn ON rn.symbol = se.symbol AND rn.session_date = se.session_date "
        "WHERE se.setup_version=?", [RUNNER_SETUP_VERSION, SETUP_VERSION]).fetchone()
    vr = con.execute(
        "WITH rn AS (SELECT DISTINCT symbol, session_date FROM setup_events WHERE setup_version=?) "
        "SELECT avg(CASE WHEN l.reached_1r_before_minus_1r THEN 1.0 ELSE 0 END), count(*) "
        "FROM setup_events se JOIN outcome_labels l ON l.setup_id = se.setup_id "
        "JOIN rn ON rn.symbol = se.symbol AND rn.session_date = se.session_date "
        "WHERE se.setup_version=?", [RUNNER_SETUP_VERSION, VR_SETUP_VERSION]).fetchone()
    vr_rate = (vr[0] or 0.0) if vr else 0.0
    vr_n = (vr[1] or 0) if vr else 0

    p_up = med_up >= RUNNER_PROMO_MEDIAN_FWDMAX_60M
    p_dd = med_dd > RUNNER_PROMO_MEDIAN_ADVERSE_60M
    p_1r = rate_1r > RUNNER_PROMO_MIN_1R
    p_beat = vr_n > 0 and rate_1r > vr_rate
    p_pm = rate_fresh > rate_1r
    mark = lambda ok: "PASS" if ok else "FAIL"
    print(f"  (1) median max-upside 60m {med_up*100:.1f}% >= {RUNNER_PROMO_MEDIAN_FWDMAX_60M*100:.0f}%  -> [{mark(p_up)}]")
    print(f"  (2) median max-drawdown 60m {med_dd*100:.1f}% > {RUNNER_PROMO_MEDIAN_ADVERSE_60M*100:.0f}%  -> [{mark(p_dd)}]")
    print(f"  (3) +1R rate {rate_1r*100:.0f}% (Wilson-95 LB {lb_1r*100:.0f}%) > {RUNNER_PROMO_MIN_1R*100:.0f}%  -> [{mark(p_1r)}]")
    print(f"  (4) beat vwap_reclaim +1R: runner {rate_1r*100:.0f}% vs vr {vr_rate*100:.0f}% (n={vr_n})  -> [{mark(p_beat)}]")
    print(f"  (5) PM-exhaustion tag earns keep: fresh +1R {rate_fresh*100:.0f}% > all {rate_1r*100:.0f}%  -> [{mark(p_pm)}]")
    print(f"  OVERALL: [{mark(p_up and p_dd and p_1r and p_beat and p_pm)}]")
    print("  NOTE: bar locked 2026-06-30 BEFORE forward data. In-sample fires are a HYPOTHESIS, "
          "not a pass — only OUT-OF-SAMPLE sessions can clear it. Do NOT gate live until cleared.")


def validate_no_chase_entry(con, target_r=2.0):
    """Final mechanic lever (shadow): does the NO-CHASE VWAP-pullback entry beat the
    ORB-high breakout in PERCENT return net of a REALISTIC per-name cost? Reuses the live
    simulate_exit on real forward bars, with the CUMULATIVE session VWAP (calculate_vwap,
    NOT minute_bars.vwap). PRE-REGISTERED bar: the no-chase entry's mean net% must be
    STRICTLY POSITIVE (out-of-sample) to promote -- 'less negative than the ORB' is NOT a pass."""
    cfg = ExitConfig(target_r=target_r)
    rows = con.execute(
        "SELECT se.symbol, se.session_date, se.setup_time, se.entry_reference_price, se.invalidation_price "
        "FROM setup_events se JOIN engineered_features f ON f.id = se.setup_id "
        "WHERE se.setup_version='v1' AND se.entry_reference_price > 0 AND se.invalidation_price > 0 "
        "AND COALESCE(f.premarket_gap_pct, f.gap_pct) >= 0.10", []).fetchall()
    nochase, orb = [], []          # (session_date, net_pct)
    universe = offered = 0
    for sym, d, st, hi, lo in rows:
        bars = con.execute(
            "SELECT timestamp, open, high, low, close, volume FROM minute_bars "
            "WHERE symbol=? AND session_date=? AND is_regular_hours ORDER BY timestamp", [sym, d]).fetchall()
        if len(bars) < 6:
            continue
        universe += 1
        df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
        for cc in ("open", "high", "low", "close", "volume"):
            df[cc] = df[cc].astype(float)
        df["vwap"] = calculate_vwap(df).values            # cumulative session VWAP
        fwd = df[df["timestamp"] > st].reset_index(drop=True)
        if not len(fwd):
            continue
        hi, lo = float(hi), float(lo)
        if hi > lo:
            r = simulate_exit(hi, lo, fwd, cfg)
            orb.append((str(d), r.r_multiple * (hi - lo) / hi - _round_trip_cost(hi)))
        ent = find_vwap_pullback_entry(fwd)
        if ent.found and ent.entry_price and ent.stop_price and ent.entry_price > ent.stop_price:
            offered += 1
            after = fwd.iloc[ent.entry_idx + 1:].reset_index(drop=True)
            if len(after):
                r = simulate_exit(ent.entry_price, ent.stop_price, after, cfg)
                nc = r.r_multiple * (ent.entry_price - ent.stop_price) / ent.entry_price
                nochase.append((str(d), nc - _round_trip_cost(ent.entry_price)))

    def mean(xs):
        return sum(v for _, v in xs) / len(xs) if xs else 0.0
    nc_mean, orb_mean = mean(nochase), mean(orb)
    print("=== NO-CHASE ENTRY shadow-validation (v1 gappers, % net of realistic per-name cost) ===")
    print(f"  cost proxy: {NOCHASE_COST_SUB3*100:.0f}% round-trip sub-$3, {NOCHASE_COST_OTHER*100:.0f}% else (no NBBO in bars)")
    print(f"  universe {universe} gappers | no-chase entry OFFERED on {offered} "
          f"({offered/universe*100:.0f}%) | traded {len(nochase)}")
    print(f"  ORB-high breakout (all gappers)   mean net% {orb_mean*100:+.2f}%")
    print(f"  NO-CHASE VWAP-pullback (when offered) mean net% {nc_mean*100:+.2f}%  "
          f"(median {sorted(v for _,v in nochase)[len(nochase)//2]*100:+.2f}%)" if nochase else "")
    # day-jackknife the no-chase mean (drop each session-day)
    days = sorted({dd for dd, _ in nochase})
    jk = [sum(v for dd, v in nochase if dd != x) / max(1, len([1 for dd, _ in nochase if dd != x]))
          for x in days]
    if jk:
        print(f"  no-chase day-jackknife mean net% range: [{min(jk)*100:+.2f}%, {max(jk)*100:+.2f}%] over {len(days)} days")
    passed = nc_mean > 0
    print(f"  PRE-REGISTERED BAR (mean net% strictly > 0): [{'PASS' if passed else 'FAIL'}]")
    print("  NOTE: in-sample over 19 autocorrelated days = a HYPOTHESIS; the bar is locked, only "
          "OUT-OF-SAMPLE forward fires can clear it. 'Less negative than ORB' is NOT a pass.")


def runner_rank_report(con):
    """Measure the cheap-price + premarket-gap SELECTION lever on the v1 ORB labeled set:
    does the runner_propensity tier separate runners (and +1R/+2R) from the base? Shadow
    measurement only -- NOT wired into live selection. Heavy small-n caveat (few gappers,
    autocorrelated days)."""
    rows = con.execute(
        "SELECT se.entry_reference_price, f.premarket_gap_pct, f.gap_pct, "
        "CASE WHEN l.reached_1r_before_minus_1r THEN 1 ELSE 0 END, "
        "CASE WHEN l.reached_2r_before_minus_1r THEN 1 ELSE 0 END, "
        "CASE WHEN l.trend_day_flag THEN 1 ELSE 0 END "
        "FROM setup_events se JOIN engineered_features f ON f.id = se.setup_id "
        "JOIN outcome_labels l ON l.setup_id = se.setup_id "
        "WHERE se.setup_version=? AND se.entry_reference_price > 0", [SETUP_VERSION]).fetchall()
    if not rows:
        print("No v1 labeled setups — run `build` first.")
        return
    scored = []
    for price, pmgap, gap, r1, r2, runner in rows:
        rp = runner_propensity(float(price),
                               gap_pct=float(gap) if gap is not None else None,
                               premarket_gap_pct=float(pmgap) if pmgap is not None else None)
        scored.append((rp, int(r1), int(r2), int(runner), float(rp.gap_used or 0.0)))
    gappers = [s for s in scored if s[0].gap_tier >= 1]            # gap >= 10%
    ng = len(gappers)
    if not ng:
        print("No gappers (gap>=10%) in the v1 set."); return

    def rate(rowset, idx):
        return sum(s[idx] for s in rowset) / len(rowset) if rowset else 0.0
    base_run, base_r1, base_r2 = rate(gappers, 3), rate(gappers, 1), rate(gappers, 2)
    print(f"=== runner-propensity SELECTION lever (v1 gappers, gap>=10%, n={ng}) ===")
    print(f"  base among gappers: runner(+20%) {base_run*100:.0f}% | +1R {base_r1*100:.0f}% | +2R {base_r2*100:.0f}%")

    def bucket(rowset, label):
        groups: dict = {}
        for s in rowset:
            groups.setdefault(label(s[0]), []).append(s)
        for key in sorted(groups):
            g = groups[key]
            lift = (rate(g, 3) / base_run) if base_run else 0
            print(f"    {key:>16}  runner {rate(g,3)*100:4.0f}%  +1R {rate(g,1)*100:3.0f}%  "
                  f"+2R {rate(g,2)*100:3.0f}%  n={len(g):3d}  runner-lift {lift:.2f}x")

    print("  by COMBINED tier (gap_tier + price_tier, higher = more runner-prone):")
    bucket(gappers, lambda rp: f"tier {rp.tier}")
    print("  by GAP tier alone (the dominant axis):")
    bucket(gappers, lambda rp: {1: "gap 10-25%", 2: "gap 25-50%", 3: "gap >=50%"}[rp.gap_tier])
    print("  by PRICE tier alone (secondary co-signal):")
    bucket(gappers, lambda rp: {0: "price >=$5", 1: "price $2-5", 2: "price <$2"}[rp.price_tier])

    # operational kicker: the live TRIGGER_GAP_MAX (~35%) excludes the strongest gappers
    excl = [s for s in gappers if s[4] > 0.35]
    kept = [s for s in gappers if s[4] <= 0.35]
    if excl:
        print(f"  GAP-CAP KICKER: the live ~35% gap cap would EXCLUDE {len(excl)} gappers whose runner "
              f"rate is {rate(excl,3)*100:.0f}% (vs {rate(kept,3)*100:.0f}% for the kept <=35%) -- "
              f"capping discards the best runners.")
    print("  CAVEAT: small n, 19 autocorrelated days; gap & price are correlated so the combined tier "
          "barely beats gap alone. A hypothesis to shadow-confirm forward, not a validated rank.")


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


def lift(con):
    """For each signal, does it SEPARATE runners/winners from the rest? Reports the
    label rate in the most-favorable bucket vs the base rate (the LIFT). n shown so
    you can tell signal from small-sample noise. NOT day-validated — the days are
    autocorrelated (one regime), so treat lift as direction-of-edge, not a win-rate."""
    rows = con.execute(
        "SELECT f.gap_pct, f.premarket_gap_pct, f.relative_volume, "
        "f.time_of_day_adjusted_relative_volume, f.distance_from_vwap, f.catalyst_type, "
        "f.metadata_json, se.above_vwap_flag, se.entry_reference_price, "
        "l.reached_1r_before_minus_1r, l.trend_day_flag "
        "FROM engineered_features f "
        "JOIN setup_events se ON se.setup_id = f.id AND se.setup_version=? "
        "JOIN outcome_labels l ON l.setup_id = f.id AND l.label_version=? "
        "WHERE f.feature_version=?", [SETUP_VERSION, LABEL_VERSION, FEATURE_VERSION]).fetchall()
    if not rows:
        print("No labeled rows — run `build` first."); return
    cols = ["gap", "pm_gap", "rvol", "tod_rvol", "dist_vwap", "cat_type", "meta",
            "above_vwap", "price", "won_1r", "runner"]
    df = pd.DataFrame(rows, columns=cols)
    df["dq"] = df["meta"].apply(lambda m: (json.loads(m).get("dq_score") if m else None))
    df["has_catalyst"] = df["cat_type"].notna()
    for c in ("won_1r", "runner", "above_vwap"):
        df[c] = df[c].astype(bool)
    n = len(df)
    NUM = [("gap", "gap%"), ("pm_gap", "premkt-gap%"), ("rvol", "rvol"),
           ("tod_rvol", "tod-rvol"), ("dist_vwap", "dist-vwap"), ("dq", "data-qual"),
           ("price", "price-$")]
    for label, lname in [("won_1r", "reached +1R"), ("runner", "+20% RUNNER")]:
        base = df[label].mean()
        pos = int(df[label].sum())
        print(f"\n=== LIFT vs label '{lname}'  (base {base*100:.1f}%, {pos} positives / {n}) ===")
        print(f"{'signal':14}{'favorable bucket':>22}{'rate':>7}{'n':>6}{'lift':>7}")
        results = []
        for feat, name in NUM:
            s = df[[feat, label]].dropna()
            if len(s) < 100:
                continue
            try:
                s = s.assign(b=pd.qcut(s[feat].rank(method="first"), 3, labels=["lo", "mid", "hi"]))
            except Exception:  # noqa: BLE001
                continue
            g = s.groupby("b", observed=True)[label].agg(["mean", "count"])
            if "hi" not in g.index or "lo" not in g.index:
                continue
            # the favorable end = whichever tercile (hi/lo) has the higher rate
            fav = "hi" if g.loc["hi", "mean"] >= g.loc["lo", "mean"] else "lo"
            rate, cnt = g.loc[fav, "mean"], int(g.loc[fav, "count"])
            results.append((f"{name} {fav}-tercile", rate, cnt, rate / base if base else 0))
        for feat, name in [("above_vwap", "above-VWAP=T"), ("has_catalyst", "has-catalyst=T")]:
            g = df.groupby(feat, observed=True)[label].agg(["mean", "count"])
            if True in g.index:
                results.append((name, g.loc[True, "mean"], int(g.loc[True, "count"]),
                                g.loc[True, "mean"] / base if base else 0))
        for nm, rate, cnt, lf in sorted(results, key=lambda x: -x[3]):
            flag = "  <- thin n" if cnt < 30 or (rate * cnt) < 8 else ""
            print(f"{'':14}{nm:>22}{rate*100:6.1f}%{cnt:>6}{lf:>6.2f}x{flag}")
        # catalyst type breakdown
        ct = df[df["cat_type"].notna()].groupby("cat_type", observed=True)[label].agg(["mean", "count"])
        ct = ct[ct["count"] >= 15].sort_values("mean", ascending=False)
        if len(ct):
            print(f"  catalyst_type (n>=15): " + " | ".join(
                f"{t}:{r['mean']*100:.0f}%/{int(r['count'])}({r['mean']/base:.1f}x)" for t, r in ct.iterrows()))
    print("\nCAVEAT: ~17-19 autocorrelated days, +20% runners are sparse — a lift is a HYPOTHESIS to")
    print("shadow-confirm forward, not a validated win-rate. Anything 'thin n' is noise until more data.")


def main():
    ap = argparse.ArgumentParser(description="offline labeler / feature store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--limit", type=int); b.add_argument("--rebuild", action="store_true")
    sub.add_parser("report")
    sub.add_parser("lift")
    bv = sub.add_parser("build-vr", help="build the vwap_reclaim shadow track")
    bv.add_argument("--limit", type=int); bv.add_argument("--rebuild", action="store_true")
    bv.add_argument("--cooldown", type=int, default=10)
    sub.add_parser("report-vr", help="forward outcomes of the vwap_reclaim shadow track")
    sub.add_parser("validate-vr", help="score the pre-registered promotion candidate vs the locked bar")
    sub.add_parser("runner-rank", help="measure the cheap-price + premarket-gap selection lever")
    sub.add_parser("validate-entry", help="shadow-validate the no-chase VWAP-pullback entry vs the locked bar")
    br = sub.add_parser("build-runner", help="build the leading_gainer_runner shadow track")
    br.add_argument("--limit", type=int); br.add_argument("--rebuild", action="store_true")
    br.add_argument("--cooldown", type=int, default=10)
    sub.add_parser("report-runner", help="forward outcomes of the leading_gainer_runner shadow track")
    sub.add_parser("validate-runner", help="score the runner track vs its locked pre-registered bar")
    args = ap.parse_args()
    con = open_research_db("market")
    if args.cmd == "build":
        build(con, limit=args.limit, rebuild=args.rebuild)
    elif args.cmd == "report":
        report(con)
    elif args.cmd == "lift":
        lift(con)
    elif args.cmd == "build-vr":
        build_vwap_reclaim(con, limit=args.limit, rebuild=args.rebuild, cooldown=args.cooldown)
    elif args.cmd == "report-vr":
        report_vwap_reclaim(con)
    elif args.cmd == "validate-vr":
        validate_vwap_reclaim(con)
    elif args.cmd == "runner-rank":
        runner_rank_report(con)
    elif args.cmd == "validate-entry":
        validate_no_chase_entry(con)
    elif args.cmd == "build-runner":
        build_runner(con, limit=args.limit, rebuild=args.rebuild, cooldown=args.cooldown)
    elif args.cmd == "report-runner":
        report_runner(con)
    elif args.cmd == "validate-runner":
        validate_runner(con)


if __name__ == "__main__":
    main()
