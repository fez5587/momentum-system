"""Leading-gainer runner detector — catch the SVRE/JEM/CELZ-class mover the
ignition detector structurally can't.

``detect_momentum_ignition`` requires a BLUE-SKY fresh-HOD (price above the prior
all-time high) — so it fires ZERO on the 2026-06-30 tape's real winners: SVRE
(+247%), JEM (+348%), CELZ (+472%) are all beaten-down / reverse-split names whose
run is a catalyst POP off a low base, nowhere near an all-time high. This detector
DROPS the blue-sky gate and grades the trait that actually defines the day's leader:
a large session run + verticality + an own-name volume burst + holding above the
session-cumulative VWAP.

It is PM-INCLUSIVE (the run that matters started pre-market) and tags — never gates
— PM-EXHAUSTION: when the pre-market already captured the bulk of the move, an RTH
entry is chasing the top (the exact reason 4/6 selection levers "separate the label
but don't convert to +1R"). The tag lets the shadow labeler measure whether skipping
spent runners lifts expectancy.

SHADOW-ONLY, same discipline as ignition/vwap-reclaim: the caller LOGS the signal and
NEVER routes it to approve_order until a pre-registered forward bar clears (median
fwd-max >= +15%, median adverse > -8% through the real bracket, positive +1R
expectancy, beats vwap-reclaim on the same sessions, and PM-exhaustion improves it).
Gates are ALL required and computable from intraday bars alone (day_base + pm_high are
optional refinements). Thresholds are params seeded from one clean in-sample split
(9 runners vs 5 chop) — a starting point to calibrate on shadow data, not a fit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from strategy.evaluation.ignition import _last_higher_low
from strategy.evaluation.levels import calculate_vwap


@dataclass
class RunnerSignal:
    is_valid: bool
    entry_level: float | None = None      # last close (the "running up" reference)
    stop_level: float | None = None       # last established higher low
    reason: str | None = None
    pm_exhausted: bool = False             # TAG (not a gate): PM took the bulk of the move
    signal_values: dict = field(default_factory=dict)


def detect_leading_gainer_runner(
    bars: pd.DataFrame,
    *,
    day_base: float | None = None,
    pm_high: float | None = None,
    min_gain: float = 0.20,
    velocity_min: float = 0.08,
    velocity_window: int = 8,
    hh_min: int = 3,
    green_min: int = 2,
    struct_lookback: int = 6,
    burst_mult: float = 1.8,
    burst_window: int = 6,
    abs_vol_floor: float = 10_000.0,
    vwap_margin: float = 0.02,
    pm_exhaustion_frac: float = 0.70,
    session_vwap: float | None = None,
) -> RunnerSignal:
    """Detect a leading-gainer runner. Pure; NO blue-sky, NO float, NO pullback.

    day_base — the reference the run is measured from (previous_close if known, else
      the first bar's open). pm_high — the pre-market high, used ONLY to tag PM
      exhaustion (never a gate). session_vwap — the cumulative session VWAP at the LAST
      bar; pass it precomputed to skip the internal recompute (cumulative VWAP is a
      PREFIX function, so vwap_full[k] == vwap over bars[:k+1] at the last bar — exact,
      not an approximation), turning a bar-by-bar slide from O(n^2) into O(n).
    """
    need = max(velocity_window + 1, struct_lookback, burst_window + 1, 8)
    if bars is None or len(bars) < need:
        return RunnerSignal(False, reason=f"need >= {need} bars")

    o = bars["open"].astype(float).to_numpy()
    h = bars["high"].astype(float).to_numpy()
    low_arr = bars["low"].astype(float).to_numpy()
    c = bars["close"].astype(float).to_numpy()
    v = bars["volume"].astype(float).to_numpy()
    last = len(c) - 1
    session_high = float(h.max())
    cum_vol = float(v.sum())
    if session_vwap is not None:
        session_vwap = float(session_vwap)
    else:
        session_vwap = float(calculate_vwap(bars).astype(float).to_numpy()[last])

    base = float(day_base) if day_base and day_base > 0 else float(o[0])

    # gate 1 — leading gainer: a large session run off the base (NO blue-sky needed)
    total_run = (session_high / base - 1.0) if base > 0 else 0.0
    is_leader = total_run >= min_gain

    # gate 2 — verticality: the "running up" acceleration
    vbase = c[last - velocity_window]
    velocity_pct = (c[last] / vbase - 1.0) if vbase > 0 else 0.0
    lookback = range(last - struct_lookback + 1, last + 1)
    green_n = sum(1 for i in lookback if c[i] > o[i])
    hh_n = sum(1 for i in lookback if h[i] > h[i - 1])
    velocity_ok = velocity_pct >= velocity_min and hh_n >= hh_min and green_n >= green_min

    # gate 3 — OWN-name volume burst + an absolute liquidity floor
    prior_mean = float(v[last - burst_window:last].mean()) if burst_window > 0 else 0.0
    burst_ratio = (v[last] / prior_mean) if prior_mean > 0 else 0.0
    volume_ok = burst_ratio >= burst_mult and cum_vol >= abs_vol_floor

    # gate 4 — holding the session-cumulative VWAP (the runner's line in the sand)
    above_vwap = c[last] >= session_vwap * (1.0 - vwap_margin)

    # TAG (not a gate) — PM exhaustion: did pre-market already take the bulk of the run?
    pm_exhausted = False
    pm_capture = None
    if pm_high is not None and base > 0:
        full_move = max(float(pm_high), session_high) - base
        pm_move = float(pm_high) - base
        if full_move > 0:
            pm_capture = pm_move / full_move
            pm_exhausted = pm_capture > pm_exhaustion_frac

    vals = {
        "total_run": round(total_run, 4),
        "velocity_pct": round(velocity_pct, 4),
        "consecutive_green": green_n,
        "higher_high_count": hh_n,
        "volume_burst_ratio": round(burst_ratio, 2),
        "cum_volume": cum_vol,
        "session_high": round(session_high, 4),
        "session_vwap": round(session_vwap, 4),
        "above_vwap": bool(above_vwap),
        "day_base": round(base, 4),
        "pm_high": float(pm_high) if pm_high is not None else None,
        "pm_capture": round(pm_capture, 4) if pm_capture is not None else None,
        "pm_exhausted": pm_exhausted,
    }

    if not is_leader:
        return RunnerSignal(False, reason="not a leading gainer (run < min_gain)",
                            pm_exhausted=pm_exhausted, signal_values=vals)
    if not velocity_ok:
        return RunnerSignal(False, reason="velocity gate failed",
                            pm_exhausted=pm_exhausted, signal_values=vals)
    if not volume_ok:
        return RunnerSignal(False, reason="volume-burst gate failed",
                            pm_exhausted=pm_exhausted, signal_values=vals)
    if not above_vwap:
        return RunnerSignal(False, reason="lost session VWAP",
                            pm_exhausted=pm_exhausted, signal_values=vals)

    return RunnerSignal(
        True,
        entry_level=round(float(c[last]), 4),
        stop_level=round(_last_higher_low(list(low_arr)), 4),
        reason="leading_gainer_runner",
        pm_exhausted=pm_exhausted,
        signal_values=vals,
    )
