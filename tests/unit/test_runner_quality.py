"""Runner-aware grade: vertical runners land A/B, chop stays F — the exact re-ranking
the default ORB grade can't do (it pins both at C/F)."""

from strategy.evaluation.runner_quality import calculate_runner_quality


def test_vertical_runner_grades_A_with_catalyst():
    # SVRE-class: +247% gap, fast, bursting, above VWAP, real catalyst
    r = calculate_runner_quality(gap_pct=2.47, velocity=0.40, vol_burst=1.8,
                                 above_vwap=True, catalyst_score=0.7)
    assert r.grade == "A" and r.score >= 0.80


def test_runner_grades_at_least_B_without_catalyst():
    # gap + velocity + VWAP do the separating structurally (catalyst OFF / Ollama down)
    r = calculate_runner_quality(gap_pct=2.47, velocity=0.40, vol_burst=1.8,
                                 above_vwap=True, catalyst_score=0.0)
    assert r.grade in ("A", "B") and r.score >= 0.65


def test_chop_stays_F():
    # the bot's 2026-06-30 chop: tiny gap, no velocity, below VWAP
    for gap, vel, burst, av in [(0.02, 0.02, 1.0, False),   # SLS-ish
                                (0.03, 0.02, 1.1, False),   # RIVN-ish
                                (0.00, 0.03, 1.2, False)]:  # CCXI-ish
        r = calculate_runner_quality(gap_pct=gap, velocity=vel, vol_burst=burst, above_vwap=av)
        assert r.grade == "F", (gap, vel, r.score)


def test_losing_vwap_drops_the_grade():
    # a runner that knifes back below session VWAP loses the 15% vwap term -> downgrades
    up = calculate_runner_quality(gap_pct=1.0, velocity=0.20, vol_burst=2.0, above_vwap=True)
    down = calculate_runner_quality(gap_pct=1.0, velocity=0.20, vol_burst=2.0, above_vwap=False)
    assert down.score < up.score and down.score == up.score - 0.15


def test_gap_saturates_at_100pct_not_10pct():
    # the default grade saturated gap at +10%, so +472% read like +11%; here +50% and
    # +472% differ, and +100%+ is maxed (a real runner isn't flattened to the chop scale)
    small = calculate_runner_quality(gap_pct=0.50, velocity=0.0, vol_burst=1.0, above_vwap=False)
    big = calculate_runner_quality(gap_pct=4.72, velocity=0.0, vol_burst=1.0, above_vwap=False)
    assert big.components["gap"] == 1.0 and small.components["gap"] == 0.5
