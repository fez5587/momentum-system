"""Runner-propensity selection signal — rank gappers by how likely they are to RUN.

The bot ranks gappers on gap%×RVOL, but the labeler proved RVOL is FLAT (~1.0x, does
not separate winners) while two FREE signals do separate runners from faders:
  - GAP MAGNITUDE (the dominant axis): premarket-gap high-tercile ~4.46x runner lift
    (labeler), and in the v1 gapper set gap>=50% runs 48% vs 21% for <50%.
  - CHEAP ABSOLUTE PRICE (a secondary, correlated co-signal): cheap<$5 runs 35% vs 17%
    for >=$5 (and the float audit's runner open median $1.19 vs fader $3.48). Big gappers
    are usually cheap microcaps, so price largely CO-OCCURS with gap and adds little on
    top of it -- it is a tie-breaker, not an independent edge.

This is a PURE, transparent, FROZEN-threshold primitive (no fitted weights, n is small
and autocorrelated). It returns a coarse runner-propensity TIER for use as a SELECTION
rank / size lever -- SHADOW/measurement first (see research.labeler runner-rank), never
a hard gate. Gap is weighted heavier than price BY CONSTRUCTION (gap_tier 0-3 vs
price_tier 0-2) because the data says gap dominates. premarket-gap is preferred over the
RTH-open gap when available (it is the stronger, earlier signal).

NOTE vs the live caps: the bot's TRIGGER_GAP_MAX (~35%) EXCLUDES exactly the gap>=50%
names that run most (it excluded NXTS at +81%). This primitive scores them HIGHEST --
the lever is as much about not CAPPING the big gappers as about ranking them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RunnerPropensity:
    tier: int                 # 0 (low) .. 5 (high) combined runner-propensity rank
    gap_tier: int             # 0-3 from gap magnitude (the dominant axis)
    price_tier: int           # 0-2 from cheapness (secondary co-signal)
    gap_used: float | None    # the gap fraction actually used
    gap_source: str           # "premarket" | "rth" | "none"
    reason: str


def runner_propensity(
    price: float | None,
    gap_pct: float | None = None,
    *,
    premarket_gap_pct: float | None = None,
    cheap_below: float = 2.0,
    mid_below: float = 5.0,
) -> RunnerPropensity:
    """Coarse runner-propensity tier from cheap-price + gap magnitude. Pure.

    tier = gap_tier (0-3) + price_tier (0-2); higher = more runner-prone. gap is the
    dominant axis (wider range); price is a secondary co-signal. premarket gap preferred."""
    # --- gap: prefer the premarket gap (stronger/earlier), fall back to the RTH gap ---
    if premarket_gap_pct is not None:
        gap, src = premarket_gap_pct, "premarket"
    elif gap_pct is not None:
        gap, src = gap_pct, "rth"
    else:
        gap, src = None, "none"

    # gap tiers (frozen bands matching the labeler's separation: >=50% is the strong cut)
    if gap is None or gap < 0.10:
        gap_tier = 0                      # not a gapper
    elif gap < 0.25:
        gap_tier = 1
    elif gap < 0.50:
        gap_tier = 2
    else:
        gap_tier = 3                      # >=50% -- the strongest runner band

    # price tiers (cheaper = higher; a secondary tie-breaker)
    if price is None or price <= 0 or price >= mid_below:
        price_tier = 0
    elif price < cheap_below:
        price_tier = 2                    # < $2
    else:
        price_tier = 1                    # $2 - $5

    tier = gap_tier + price_tier
    reason = (f"gap_tier={gap_tier}({src}{'' if gap is None else f' {gap*100:.0f}%'})"
              f"+price_tier={price_tier}")
    return RunnerPropensity(tier=tier, gap_tier=gap_tier, price_tier=price_tier,
                            gap_used=gap, gap_source=src, reason=reason)
