"""No-chase entry primitive — buy the VWAP reclaim, not the breakout high.

The mechanics study (2026-06-29, wf_bdff509d) found that the ORB-high breakout entry
CHASES the spike-top: from it only ~8% residual upside remains and it whipsaws out 65%
of the time. Not chasing — entering on the first pullback that RECLAIMS the cumulative
session VWAP — was the single most powerful mechanic (beat the breakout entry by ~4.4pt
net%, 2.5-5x more than any stop/exit tweak) because it SHRINKS per-trade risk (the stop
sits at the pullback low, ~1.6% away, vs the ORB stop ~7% away) instead of fighting the
runner/fader tradeoff.

This is a PURE primitive: given the forward bars after the ORB decision point (each
carrying the CUMULATIVE SESSION VWAP, not the per-bar vwap), return the first bar that
dips to/through VWAP and closes back above it -- entry = that close, stop = that bar's
low. SHADOW/validation only (research.labeler validate-entry); not wired into live entry.

IMPORTANT: the `vwap` column MUST be the cumulative session VWAP (calculate_vwap over the
session from the open), NOT minute_bars.vwap (a per-bar typical price ~midpoint, for which
low<=vwap<=close is trivially almost always true). The caller is responsible for that.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class PullbackEntry:
    found: bool
    entry_price: float | None = None   # the reclaim bar's close
    stop_price: float | None = None    # the reclaim bar's low (the no-chase tight stop)
    entry_idx: int | None = None       # index into the forward bars
    reason: str = ""


def find_vwap_pullback_entry(bars: pd.DataFrame) -> PullbackEntry:
    """First forward bar that pulls back to the cumulative VWAP and reclaims it
    (low <= vwap <= close): enter at its close, stop at its low. Pure. `bars` must
    carry the CUMULATIVE SESSION VWAP in column `vwap` (see module note)."""
    if bars is None or len(bars) == 0:
        return PullbackEntry(False, reason="no bars")
    b = bars.reset_index(drop=True)
    low = b["low"].astype(float)
    close = b["close"].astype(float)
    vwap = b["vwap"].astype(float)
    for i in range(len(b)):
        v = float(vwap.iloc[i])
        if v <= 0:
            continue
        lo, cl = float(low.iloc[i]), float(close.iloc[i])
        if lo <= v <= cl and cl > lo:          # touched VWAP from above, closed back over it
            return PullbackEntry(True, entry_price=cl, stop_price=lo, entry_idx=i,
                                 reason="vwap_reclaim")
    return PullbackEntry(False, reason="no vwap reclaim")
