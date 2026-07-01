"""Premarket setup detector — the ONE axis the six refuted RTH levers never touched.

The source trader's edge is 7:00-9:30 ET premarket, by his own account: news drops pre/after
hours, there are NO circuit-breaker halts pre-market so news-driven moves run clean, and RTH
after 9:30 is "choppy... not predictable" (HFT gaming stop/market orders) — the exact window our
RTH bot trades and keeps getting chopped in. His winners (e.g. CF: 16x RVOL, VWAP-reclaim curl)
share a shape; his losers (DXST: breaking news but LOW premarket volume, gave it all back) fail
the volume test. So this detector operates on PREMARKET bars and gates on the traits that defined
his premarket winners:
  G1 GAP        — premarket high vs prev_close >= min_gap (a real news-driven mover, not noise).
  G2 VOLUME     — cumulative premarket volume >= min_pm_volume (real participation; the DXST
                  failure was low premarket volume — the single cheapest tell).
  G3 PRICE FLOOR— price >= price_min (the trader's pillar: he skips the too-cheap names).
  G4 BREAKOUT   — last bar at/near the premarket session high (momentum continuation / the break).
  G5 VWAP HOLD  — close >= premarket-cumulative VWAP (the reclaim he enters on).
NO circuit-breaker / halt logic needed (none exist pre-market — that's the whole point).
stop = last established higher low.

SHADOW-ONLY, same discipline as every other detector: the caller LOGS the signal and NEVER routes
it to approve_order. Premarket MIGHT convert where RTH didn't (cleaner no-halt moves), or hit the
same entry-conversion wall, or be un-fillable in paper — only a forward backtest on premarket bars
decides. Live premarket execution additionally needs extended_hours=true LIMIT orders (a code
change the bot doesn't have yet). Thresholds are seeded, not fitted — calibrate on shadow data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from strategy.evaluation.ignition import _last_higher_low
from strategy.evaluation.levels import calculate_vwap


@dataclass
class PremarketSignal:
    is_valid: bool
    entry_level: float | None = None      # last close (the break/reclaim reference)
    stop_level: float | None = None       # last established higher low
    reason: str | None = None
    signal_values: dict = field(default_factory=dict)


def detect_premarket_setup(
    bars: pd.DataFrame,
    *,
    prev_close: float | None = None,
    min_gap: float = 0.10,
    min_pm_volume: float = 20_000.0,   # premarket is THIN (real-tape p50 ~12k cum shares); this
                                       # catches the genuine movers (JEM/CELZ) not the 1%-gap noise
    price_min: float = 1.5,
    breakout_tol: float = 0.005,
    vwap_margin: float = 0.02,
    velocity_window: int = 8,
    velocity_min: float = 0.03,
    session_vwap: float | None = None,
) -> PremarketSignal:
    """Detect a premarket continuation setup. Pure. `bars` must be PREMARKET bars only.
    session_vwap — premarket-cumulative VWAP at the last bar; pass precomputed for an O(n) slide."""
    need = max(velocity_window + 1, 10)
    if bars is None or len(bars) < need:
        return PremarketSignal(False, reason=f"need >= {need} bars")

    o = bars["open"].astype(float).to_numpy()
    h = bars["high"].astype(float).to_numpy()
    low_arr = bars["low"].astype(float).to_numpy()
    c = bars["close"].astype(float).to_numpy()
    v = bars["volume"].astype(float).to_numpy()
    last = len(c) - 1
    pm_high = float(h.max())
    price = float(c[last])
    cum_vol = float(v.sum())
    base = float(prev_close) if prev_close and prev_close > 0 else float(o[0])

    gap = (pm_high / base - 1.0) if base > 0 else 0.0
    vbase = c[last - velocity_window]
    velocity = (c[last] / vbase - 1.0) if vbase > 0 else 0.0
    breakout = price >= pm_high * (1.0 - breakout_tol)
    if session_vwap is not None:
        vwap_last = float(session_vwap)
    else:
        vwap_last = float(calculate_vwap(bars).astype(float).to_numpy()[last])
    above_vwap = price >= vwap_last * (1.0 - vwap_margin)

    vals = {
        "gap": round(gap, 4), "velocity": round(velocity, 4), "cum_pm_volume": round(cum_vol, 0),
        "price": round(price, 4), "pm_high": round(pm_high, 4), "pm_vwap": round(vwap_last, 4),
        "above_vwap": bool(above_vwap), "is_breakout": bool(breakout),
    }

    if gap < min_gap:
        return PremarketSignal(False, reason="gap below floor", signal_values=vals)
    if cum_vol < min_pm_volume:
        return PremarketSignal(False, reason="thin premarket volume", signal_values=vals)
    if price < price_min:
        return PremarketSignal(False, reason="below price floor", signal_values=vals)
    if velocity < velocity_min:
        return PremarketSignal(False, reason="no premarket velocity", signal_values=vals)
    if not breakout:
        return PremarketSignal(False, reason="not at premarket high", signal_values=vals)
    if not above_vwap:
        return PremarketSignal(False, reason="below premarket VWAP", signal_values=vals)

    return PremarketSignal(
        True,
        entry_level=round(price, 4),
        stop_level=round(_last_higher_low(list(low_arr)), 4),
        reason="premarket_setup",
        signal_values=vals,
    )
