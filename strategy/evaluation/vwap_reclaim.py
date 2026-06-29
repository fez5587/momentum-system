"""VWAP-reclaim continuation detector — the trader's #1 "first-pullback" setup.

The bot's live setup is the open-anchored opening-range breakout (fires once,
early, off the day's open, never re-arms). The trader's actual edge — and his
explicitly-taught highest-conviction setup — is a CONTINUATION: a leading gainer
on a news catalyst that SPIKED, PULLED BACK (holding >=50% of the run), and is
now CURLING BACK UP through VWAP, bought BELOW the high-of-day (not at the top).
Across 2026-06-25..29 every name he monetized (MIMI/FCUV/ZDAI/SDOT/UPC) was this
one pattern, and the bot blocked all of them on the ORB score gate.

This is a standalone, pure, binary-rule detector — NOT wired into classify_setup
(whose pullback criteria are HOD/new-high anchored, the opposite of buy-the-curl).
SHADOW-ONLY: the caller logs the signal and never trades it until the labeler
proves it tags real +1R curls. Reuses calculate_vwap / calculate_ema from levels
(no train/serve skew). Every input is already computed by the live system.

Gates (ALL required):
  1. IMPULSE        — the stock ran up >= min_run_pct off its session base.
  2. PULLBACK HELD  — there's a pullback low AFTER the HOD that held >= hold_frac
                      (default 0.5) of the run (didn't give the move back).
  3. CURL / RECLAIM — the latest bar makes a NEW HIGH vs the pullback range AND
                      is back at/above VWAP (the trader's "first candle to make a
                      new high, curling up through VWAP").
  4. BELOW HOD      — latest close <= HOD*(1-hod_margin): buying the curl, not the
                      top (directly the anti-spike-top guard).
  5. MOMENTUM GREEN — ema9 >= ema20 (the MACD-green proxy; full MACD logged as a
                      tag). MACD has no impl in the codebase but EMAs are computed.

stop = the pullback low (the trader's "low of the pullback"); target = HOD retest;
the entry-to-stop vs target R:R is logged (he wants >= 2:1). Thresholds are params
seeded reasonably — CALIBRATE on shadow/labeler data, do NOT hand-fit to the few
named anecdotes (n is tiny).

P0 REAL-DATA FINDING (2026-06-29, RTH minute bars):
  Slid across 4 real trader names. FIRED on the all-day TRENDERS — SDOT 6/26
  (10.22->22.50) 22x, FCUV 6/25 2x — but ZERO on MIMI 6/25 and UPC 6/29, BOTH
  rejected on "pullback broke past the hold level" with a NEGATIVE held_frac
  (-1.12 / -0.34). Root cause is STRUCTURAL, not a threshold: this detector
  anchors the impulse to the day's SINGLE HOD and the session-absolute low before
  it. A trender's HOD keeps advancing so its pullbacks hold; but a name that
  spikes, makes HOD, then FADES has every later bar read as "broke the hold"
  forever -- so the detector is blind to the LOCAL spike->pullback->curl structures
  that re-arm through the day (exactly the "first pullback off each leg" the trader
  takes). P1 FIX: anchor to a ROLLING local-swing window, not the day's one HOD.
  (Do NOT widen hold_frac to force MIMI/UPC green -- that's fitting n=4 noise; the
  generalizing fix is the local anchor, validated later on many shadow signals.)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from strategy.evaluation.levels import calculate_ema, calculate_vwap


@dataclass
class VwapReclaimSignal:
    is_valid: bool
    breakout_level: float | None = None   # entry reference (the curl level)
    stop_level: float | None = None       # the pullback low
    target_level: float | None = None     # HOD retest
    reason: str | None = None
    signal_values: dict = field(default_factory=dict)


def _macd_hist(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> float:
    """MACD line minus signal line on the last bar (>0 = bullish). Logged as a tag
    for the shadow comparison; the live gate uses the ema9>=ema20 proxy."""
    macd = calculate_ema(close, fast) - calculate_ema(close, slow)
    sig = calculate_ema(macd, signal)
    if macd.empty or sig.empty:
        return 0.0
    return float(macd.iloc[-1] - sig.iloc[-1])


def detect_vwap_reclaim(
    bars: pd.DataFrame,
    *,
    min_run_pct: float = 0.10,
    hold_frac: float = 0.5,
    hod_margin: float = 0.02,
    min_bars: int = 21,
) -> VwapReclaimSignal:
    """Detect a VWAP-reclaim continuation (first-pullback curl). Pure."""
    if bars is None or len(bars) < min_bars:
        return VwapReclaimSignal(False, reason=f"need >= {min_bars} bars")
    bars = bars.reset_index(drop=True)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    close = bars["close"].astype(float)
    vwap = calculate_vwap(bars)
    ema9 = calculate_ema(close, 9)
    ema20 = calculate_ema(close, 20)
    last = len(bars) - 1

    hod = float(high.max())
    hod_idx = int(high.idxmax())
    # a pullback (and a curl bar) must exist AFTER the HOD -- else we're at/near the
    # top, which is exactly the spike-top entry we must avoid.
    if hod_idx > last - 2:
        return VwapReclaimSignal(False, reason="at/near HOD (no pullback to curl from)")

    run_low = float(low.iloc[:hod_idx + 1].min())          # base the impulse ran from
    run = hod - run_low
    ran = run_low > 0 and run / run_low >= min_run_pct
    pb_low_idx = int(low.iloc[hod_idx + 1:].idxmin())      # the pullback bottom
    pullback_low = float(low.iloc[pb_low_idx])             # low after the HOD
    held = run > 0 and pullback_low >= run_low + hold_frac * run

    # the curl: the latest bar makes a fresh high SINCE THE PULLBACK LOW (the curl
    # leg resuming up — not vs the whole pullback, whose first bar sits at the HOD),
    # has recovered off the low, and is back at/above VWAP — but still below the HOD.
    still_falling = pb_low_idx >= last
    curl_high_prev = float(high.iloc[pb_low_idx:last].max()) if not still_falling else hod
    new_high = (not still_falling) and float(high.iloc[last]) > curl_high_prev
    recovered = float(close.iloc[last]) > float(close.iloc[pb_low_idx])
    back_above_vwap = float(close.iloc[last]) >= float(vwap.iloc[last])
    crossed_vwap_up = float(close.iloc[last - 1]) < float(vwap.iloc[last - 1]) and back_above_vwap
    below_hod = float(close.iloc[last]) <= hod * (1.0 - hod_margin)
    green = float(ema9.iloc[last]) >= float(ema20.iloc[last])

    entry = float(close.iloc[last])
    rr = ((hod - entry) / (entry - pullback_low)) if entry > pullback_low else 0.0
    vals = {
        "run_pct": round(run / run_low, 4) if run_low > 0 else None,
        "pullback_low": round(pullback_low, 4),
        "pullback_held_frac": round((pullback_low - run_low) / run, 3) if run > 0 else None,
        "hod": round(hod, 4),
        "dist_from_vwap": round(entry - float(vwap.iloc[last]), 4),
        "below_hod_pct": round((hod - entry) / hod, 4) if hod > 0 else None,
        "ema9_ge_ema20": bool(green),
        "macd_hist": round(_macd_hist(close), 5),
        "crossed_vwap_up": bool(crossed_vwap_up),
        "target_rr": round(rr, 2),
    }

    if not ran:
        return VwapReclaimSignal(False, reason="no impulse (run < min_run_pct)", signal_values=vals)
    if not held:
        return VwapReclaimSignal(False, reason="pullback broke past the hold level", signal_values=vals)
    if not (new_high and recovered and back_above_vwap):
        return VwapReclaimSignal(False, reason="not curling up through VWAP", signal_values=vals)
    if not below_hod:
        return VwapReclaimSignal(False, reason="at/too near HOD (not the curl)", signal_values=vals)
    if not green:
        return VwapReclaimSignal(False, reason="momentum not green (ema9 < ema20)", signal_values=vals)

    return VwapReclaimSignal(
        True,
        breakout_level=round(entry, 4),
        stop_level=round(pullback_low, 4),
        target_level=round(hod, 4),
        reason="vwap_reclaim",
        signal_values=vals,
    )
