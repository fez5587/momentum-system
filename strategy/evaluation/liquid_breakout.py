"""Liquid intraday-RVOL breakout detector — catch the LIQUID intraday igniter the
premarket-gapper universe is blind to, and that the runner detector fired on too thinly.

Motivation (2026-07-01 forensic + the source trader's day-14 notes):
- The day's only clean, held, TRADEABLE +2R moves (MSTR, SSPC) were liquid names that
  ignited INTRADAY with ~zero premarket gap — invisible to a premarket-gap scan.
- The cheap "held trenders" (AMCI/CCTG/…) were untradeable dust: $3-27k whole-day
  dollar-volume, single-print spikes. Their "held 100%" was a no-liquidity artifact.
- The trader's WINNER (CF) had 16x RVOL + 24M volume + a VWAP-reclaim curl; his LOSER
  (DXST) had breaking news but LOW rvol/volume and gave it all back. RVOL + real
  dollar-volume + VWAP-hold is the recipe; news alone and cheap-price alone are not.

So this detector makes LIQUIDITY a first-class gate (the thing runner.py lacked, which let
it fire on dust) and DROPS the gap requirement (the thing that hid MSTR/SSPC). Gates (all
required, PM-inclusive session-to-date):
  G1 LIQUIDITY  — cumulative session dollar-volume >= min_dollar_vol AND recent per-bar
                  depth (median of last K bars) >= min_bar_dollar_vol. "Can I fill in size?"
  G2 PRICE FLOOR— price >= price_min (avoid the too-cheap dust the trader's pillars exclude
                  and that our runner-propensity audit found separates label but not edge).
  G3 BREAKOUT   — the last bar prints at/near a new session high (the ignition trigger).
  G4 VWAP HOLD  — close >= session-cumulative VWAP * (1 - vwap_margin) (the trader's reclaim).
NO gap requirement, NO blue-sky, NO float. stop = last established higher low.

RVOL IS RECORDED, NOT A HARD GATE. Validation on 2026-07-01 showed the day's actual tradeable
liquid movers (MSTR, SSPC, both held +2R) were NOT within-session volume surges — MSTR was liquid
from the open and its recent/early volume ratio sat at ~0.5-1.0 the whole grind. A hard rvol>=3x
gate (a "surge" concept borrowed from thin premarket gappers) structurally kills steadily-liquid
grinders — exactly the lane this detector exists to catch. So rvol is computed and STORED as a
stratification feature (the shadow track measures whether high-rvol fires convert better, like the
runner GRADE), and only becomes a gate if rvol_min>0 is passed explicitly.

SHADOW-ONLY, same discipline as ignition/vwap_reclaim/runner: the caller LOGS the signal and
NEVER routes it to approve_order until a pre-registered forward bar clears. This is the 6th
selection signal to separate the runner label; it must PROVE it converts after cost before
any live gating. Thresholds are seeded, not fitted — calibrate on shadow data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from strategy.evaluation.ignition import _last_higher_low
from strategy.evaluation.levels import calculate_vwap


@dataclass
class LiquidBreakoutSignal:
    is_valid: bool
    entry_level: float | None = None      # last close (the breakout reference)
    stop_level: float | None = None       # last established higher low
    reason: str | None = None
    signal_values: dict = field(default_factory=dict)


def detect_liquid_intraday_breakout(
    bars: pd.DataFrame,
    *,
    min_dollar_vol: float = 1_000_000.0,   # cumulative session $-volume floor ("fillable")
    min_bar_dollar_vol: float = 5_000.0,   # median recent per-bar $-volume (real depth)
    rvol_min: float = 0.0,                 # RECORDED not gated (0 = off); see module docstring
    rvol_recent: int = 5,                  # bars in the "recent" window
    rvol_base_min: int = 10,               # min bars of early-session baseline
    breakout_tol: float = 0.005,           # within 0.5% of session high counts as breakout
    vwap_margin: float = 0.02,
    price_min: float = 2.0,
    session_vwap: float | None = None,
) -> LiquidBreakoutSignal:
    """Detect a liquid intraday-RVOL breakout. Pure; NO gap, NO float, NO blue-sky.

    session_vwap — cumulative VWAP at the last bar; pass precomputed to skip the internal
    recompute (prefix identity, O(n) sliding — same trick as runner.py).
    """
    need = max(rvol_recent + rvol_base_min, 12)
    if bars is None or len(bars) < need:
        return LiquidBreakoutSignal(False, reason=f"need >= {need} bars")

    h = bars["high"].astype(float).to_numpy()
    low_arr = bars["low"].astype(float).to_numpy()
    c = bars["close"].astype(float).to_numpy()
    v = bars["volume"].astype(float).to_numpy()
    last = len(c) - 1
    session_high = float(h.max())
    price = float(c[last])

    # dollar-volume per bar + cumulative
    dvol_bar = c * v
    cum_dollar = float(dvol_bar.sum())
    recent_bar_depth = float(pd.Series(dvol_bar[max(0, last - rvol_recent + 1):last + 1]).median())

    # G1 liquidity — can we fill in size?
    liquid = cum_dollar >= min_dollar_vol and recent_bar_depth >= min_bar_dollar_vol

    # rvol — recorded stratification feature (hard gate only if rvol_min>0; see docstring)
    recent_vol = float(v[last - rvol_recent + 1:last + 1].mean())
    base_vol = float(v[:last - rvol_recent + 1].mean()) if last - rvol_recent + 1 >= 1 else 0.0
    rvol = (recent_vol / base_vol) if base_vol > 0 else 0.0
    rvol_ok = rvol_min <= 0.0 or rvol >= rvol_min

    # G3 breakout — last bar at/near a fresh session high
    breakout = price >= session_high * (1.0 - breakout_tol)

    # G4 vwap hold
    if session_vwap is not None:
        vwap_last = float(session_vwap)
    else:
        vwap_last = float(calculate_vwap(bars).astype(float).to_numpy()[last])
    above_vwap = price >= vwap_last * (1.0 - vwap_margin)

    # G5 price floor
    price_ok = price >= price_min

    vals = {
        "cum_dollar_vol": round(cum_dollar, 0),
        "recent_bar_depth": round(recent_bar_depth, 0),
        "rvol": round(rvol, 2),
        "price": round(price, 4),
        "session_high": round(session_high, 4),
        "session_vwap": round(vwap_last, 4),
        "above_vwap": bool(above_vwap),
        "is_breakout": bool(breakout),
    }

    if not liquid:
        return LiquidBreakoutSignal(False, reason="illiquid (dollar-volume floor)", signal_values=vals)
    if not price_ok:
        return LiquidBreakoutSignal(False, reason="below price floor", signal_values=vals)
    if not rvol_ok:
        return LiquidBreakoutSignal(False, reason="no rvol surge", signal_values=vals)
    if not breakout:
        return LiquidBreakoutSignal(False, reason="not at session high", signal_values=vals)
    if not above_vwap:
        return LiquidBreakoutSignal(False, reason="below session VWAP", signal_values=vals)

    return LiquidBreakoutSignal(
        True,
        entry_level=round(price, 4),
        stop_level=round(_last_higher_low(list(low_arr)), 4),
        reason="liquid_intraday_breakout",
        signal_values=vals,
    )
