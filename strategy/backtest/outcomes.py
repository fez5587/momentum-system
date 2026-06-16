"""Outcome labeling for setups (R-multiple based)."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class OutcomeLabel:
    """Outcome of a setup given subsequent bars."""

    setup_id: str
    hit_target: bool
    hit_stop: bool
    r_multiple: float
    max_favorable_r: float
    max_adverse_r: float
    bars_to_resolution: int
    label: str  # "win" | "loss" | "scratch" | "unresolved"


def label_outcome(
    setup_id: str,
    entry_price: float,
    stop_price: float,
    bars_after: pd.DataFrame,
    target_r: float = 2.0,
) -> OutcomeLabel:
    """Walk forward through bars to see whether target or stop hit first."""
    risk = entry_price - stop_price
    if risk <= 0 or bars_after.empty:
        return OutcomeLabel(setup_id, False, False, 0.0, 0.0, 0.0, 0, "unresolved")

    target_price = entry_price + target_r * risk
    max_fav = 0.0
    max_adv = 0.0

    for i, (_, bar) in enumerate(bars_after.iterrows(), start=1):
        high = float(bar["high"])
        low = float(bar["low"])
        max_fav = max(max_fav, (high - entry_price) / risk)
        max_adv = min(max_adv, (low - entry_price) / risk)

        stop_hit = low <= stop_price
        target_hit = high >= target_price
        if stop_hit and target_hit:
            # Conservative: assume stop hit first within the bar
            return OutcomeLabel(
                setup_id, False, True, -1.0, max_fav, max_adv, i, "loss"
            )
        if stop_hit:
            return OutcomeLabel(
                setup_id, False, True, -1.0, max_fav, max_adv, i, "loss"
            )
        if target_hit:
            return OutcomeLabel(
                setup_id, True, False, target_r, max_fav, max_adv, i, "win"
            )

    final_r = (float(bars_after["close"].iloc[-1]) - entry_price) / risk
    label = "scratch" if abs(final_r) < 0.25 else ("win" if final_r > 0 else "loss")
    return OutcomeLabel(
        setup_id, False, False, round(final_r, 3), max_fav, max_adv,
        len(bars_after), label,
    )
