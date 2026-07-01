"""Runner-aware setup grade — score a VERTICAL leading-gainer sensibly.

The default calculate_setup_quality (quality.py) pins catalyst RUNNERS at F/C because
two of its components structurally break on a vertical spike:
  - STRUCTURE (30% weight): classify_setup returns NONE on a vertical impulse (no clean
    pullback), so the biggest component reads 0.
  - adv-RVOL (25%): rolling_avg_volume_20d is NULL for fresh reverse-split runners, so
    relative_volume reads 1.0x -> ~0.2. (This is the exact SVRE "grades F at 0.35" bug.)
On the 2026-06-30 tape the source trader's winners (SVRE +247%, JEM +348%, CELZ +472%)
all graded C 0.565 while the bot's F-grade chop scored the same range — the grade could
not tell a runner from noise.

This is a PARALLEL scorer (quality.py is untouched, its ORB grading/tests unchanged). It
DROPS the two broken components and grades on the traits that actually separate runners
from chop on real bars: session gap, price VELOCITY (the "running up" acceleration),
a RELATIVE volume burst (vs the name's OWN recent bars, never adv-RVOL or raw cum-volume
— which is anti-signal: the liquid chop dwarfs the thin runners), above-VWAP, and the
catalyst tag. Weights are COARSE and seeded from one day's clean in-sample split (n=9
runners vs 5 chop) — a starting point to CALIBRATE on shadow data, not a fitted model.
It RANKS runners; it does not authorize entry (a runner that then loses VWAP grades F,
but a well-graded bar can still bleed — see the runner detector's shadow-only discipline).
"""

from __future__ import annotations

from strategy.evaluation.quality import QualityScore, QualityThresholds


def calculate_runner_quality(
    gap_pct: float,
    velocity: float,
    vol_burst: float,
    above_vwap: bool,
    catalyst_score: float = 0.0,
    data_quality: float = 1.0,
    thresholds: QualityThresholds | None = None,
) -> QualityScore:
    """Blend runner traits into one 0..1 grade. Pure. Inputs:
      gap_pct     — session_high/prev_close - 1 (saturates at +100%, NOT +10%, so a
                    +472% runner doesn't read the same as +11%).
      velocity    — the "running up" signal: max recent N-bar return (saturates at +20%).
      vol_burst   — recent-vol / prior-vol on the name's OWN bars (relative, saturates 3x).
      above_vwap  — price at/above the session-cumulative VWAP (the safety: a name that
                    loses VWAP drops this to 0 and the grade falls).
      catalyst_score — 0..1 LLM catalyst read; degrades to 0 gracefully (Ollama gated OFF).
    """
    thresholds = thresholds or QualityThresholds()

    def _clip(x: float) -> float:
        return min(1.0, max(0.0, x))

    gap_c = _clip(gap_pct / 1.0)          # saturate at +100%
    vel_c = _clip(velocity / 0.20)        # saturate at +20% over the velocity window
    avol_c = _clip(vol_burst / 3.0)       # saturate at a 3x burst
    vwap_c = 1.0 if above_vwap else 0.0
    cat_c = _clip(catalyst_score)

    components = {
        "gap": gap_c, "velocity": vel_c, "volume_burst": avol_c,
        "vwap": vwap_c, "catalyst": cat_c,
    }
    score = (0.30 * gap_c + 0.25 * vel_c + 0.15 * avol_c
             + 0.15 * vwap_c + 0.15 * cat_c) * _clip(data_quality)

    if score >= thresholds.a_grade:
        grade = "A"
    elif score >= thresholds.b_grade:
        grade = "B"
    elif score >= thresholds.c_grade:
        grade = "C"
    else:
        grade = "F"

    return QualityScore(score=round(score, 4), grade=grade, components=components,
                        tradeable=score >= thresholds.min_tradeable)
