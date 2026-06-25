"""Momentum-ignition detector — a SECOND setup the pullback-ORB scorer is blind to.

A vertical squeeze (PLSM-class: blue-sky, volume explosion, NO pullback) makes
``strategy.evaluation.structure.classify_setup`` return NONE — it requires a
consolidation/pullback phase a true parabola never gives — so the bot blocks it
(PLSM 2026-06-24 was evaluated 1,895x and blocked ~30% every time). This is a
standalone, pure, binary-rule detector. It is deliberately NOT wired into
classify_setup / evaluate_setup (those pullback criteria would reject it, and
STRATEGY_SETUPS would filter it). SHADOW-ONLY: the caller logs the signal and
NEVER trades it until forward data proves runner-vs-trap separation.

Gates (ALL required; computable from intraday bars + a prior all-time high):
  1. BLUE-SKY FRESH-HOD     — the latest bar prints a NEW session high AND that
     high is above the prior all-time high (no overhead resistance, room to run).
  2. PRICE VELOCITY         — +velocity_min over velocity_window bars, with
     >= green_min of the last green_lookback bars green and rising highs.
  3. ABSOLUTE VOLUME BURST  — last-bar volume >= burst_mult x the prior K-bar
     mean, AND cumulative session volume >= abs_vol_floor. ABSOLUTE floors, NOT
     RVOL: scan_gappers RVOL zeros out on thin IPOs (avg-daily-vol = 0) and float
     is NULL — that blind spot is exactly why PLSM read None mid-ignition.

stop = the last established HIGHER LOW beneath the ignition bar (ride the up-
structure). breakout_level = the fresh-HOD trigger. Catalyst / VWAP / float are
TAGS the caller attaches for grading — never gates here. Thresholds are params
(env knobs at the call site), seeded from one PLSM window; calibrate only after
shadow data accumulates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class IgnitionSignal:
    is_valid: bool
    breakout_level: float | None = None   # fresh-HOD trigger (entry reference)
    stop_level: float | None = None       # last established higher low
    reason: str | None = None
    signal_values: dict = field(default_factory=dict)


def _last_higher_low(lows: list[float]) -> float:
    """Ride the stop under the last established HIGHER LOW *beneath* the final
    (ignition) bar: scan back from the 2nd-to-last bar for the most recent bar
    whose low is higher than the bar before it. Fallback: the window's min low."""
    for i in range(len(lows) - 2, 0, -1):
        if lows[i] > lows[i - 1]:
            return lows[i]
    return min(lows) if lows else 0.0


def detect_momentum_ignition(
    bars: pd.DataFrame,
    *,
    prior_ath: float | None = None,
    velocity_min: float = 0.08,
    velocity_window: int = 8,
    green_min: int = 4,
    green_lookback: int = 5,
    burst_mult: float = 3.0,
    burst_window: int = 5,
    abs_vol_floor: float = 100_000.0,
) -> IgnitionSignal:
    """Detect a vertical momentum ignition. Pure; no pullback required."""
    need = max(velocity_window + 1, green_lookback, burst_window + 1)
    if bars is None or len(bars) < need:
        return IgnitionSignal(False, reason=f"need >= {need} bars")

    o = bars["open"].astype(float).to_numpy()
    h = bars["high"].astype(float).to_numpy()
    low_arr = bars["low"].astype(float).to_numpy()
    c = bars["close"].astype(float).to_numpy()
    v = bars["volume"].astype(float).to_numpy()
    last = len(c) - 1
    session_high = float(h.max())
    cum_vol = float(v.sum())

    # gate 1 — blue-sky fresh-HOD
    new_hod = bool(h[last] >= session_high)                       # final bar made the high
    blue_sky = prior_ath is not None and session_high > float(prior_ath)

    # gate 2 — price velocity / verticality
    base = c[last - velocity_window]
    velocity_pct = (c[last] / base - 1.0) if base > 0 else 0.0
    lookback = range(last - green_lookback + 1, last + 1)
    green_n = sum(1 for i in lookback if c[i] > o[i])
    hh_n = sum(1 for i in range(last - green_lookback + 1, last + 1) if h[i] > h[i - 1])
    velocity_ok = (velocity_pct >= velocity_min and green_n >= green_min
                   and hh_n >= green_min)

    # gate 3 — absolute volume burst (NOT rvol)
    prior_mean = float(v[last - burst_window:last].mean()) if burst_window > 0 else 0.0
    burst_ratio = (v[last] / prior_mean) if prior_mean > 0 else 0.0
    volume_ok = burst_ratio >= burst_mult and cum_vol >= abs_vol_floor

    vals = {
        "velocity_pct": round(velocity_pct, 4),
        "consecutive_green": green_n,
        "higher_high_count": hh_n,
        "volume_burst_ratio": round(burst_ratio, 2),
        "cum_volume": cum_vol,
        "session_high": round(session_high, 4),
        "prior_ath": float(prior_ath) if prior_ath is not None else None,
        "is_blue_sky": bool(blue_sky),
        "new_hod": new_hod,
    }

    if not (new_hod and blue_sky):
        return IgnitionSignal(False, reason="not blue-sky fresh-HOD", signal_values=vals)
    if not velocity_ok:
        return IgnitionSignal(False, reason="velocity gate failed", signal_values=vals)
    if not volume_ok:
        return IgnitionSignal(False, reason="volume-burst gate failed", signal_values=vals)

    return IgnitionSignal(
        True,
        breakout_level=round(session_high, 4),       # the fresh HOD = entry reference
        stop_level=round(_last_higher_low(list(low_arr)), 4),
        reason="momentum_ignition",
        signal_values=vals,
    )
