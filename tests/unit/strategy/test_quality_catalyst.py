"""Phase 2: optional catalyst_score blend stays pure & backward-compatible."""

import pytest

from strategy.evaluation.quality import calculate_setup_quality


_KW = dict(
    gap_pct=0.08,
    relative_volume=3.0,
    structure_quality=0.7,
    above_vwap=True,
    opening_strength="strong",
    data_quality=1.0,
)


def test_none_catalyst_reproduces_legacy_score():
    # The exact pre-catalyst weighting (gap .20 / rvol .25 / structure .30 /
    # vwap .10 / opening .05 / data .10) must be unchanged when score is None.
    q = calculate_setup_quality(**_KW, catalyst_score=None)
    expected = (
        0.20 * 0.8 + 0.25 * 0.6 + 0.30 * 0.7 + 0.10 * 1.0
        + 0.05 * 1.0 + 0.10 * 1.0
    )
    assert q.score == pytest.approx(round(expected, 4))
    assert "catalyst" not in q.components


def test_default_arg_matches_explicit_none():
    assert calculate_setup_quality(**_KW).score == calculate_setup_quality(
        **_KW, catalyst_score=None
    ).score


def test_strong_catalyst_raises_score_and_adds_component():
    base = calculate_setup_quality(**_KW, catalyst_score=None).score
    high = calculate_setup_quality(**_KW, catalyst_score=1.0)
    assert high.score > base
    assert high.components["catalyst"] == 1.0


def test_zero_catalyst_lowers_score_vs_legacy():
    # re-balancing toward a 0 catalyst pulls a strong setup down a touch
    base = calculate_setup_quality(**_KW, catalyst_score=None).score
    weak = calculate_setup_quality(**_KW, catalyst_score=0.0).score
    assert weak < base


def test_catalyst_weights_sum_to_one():
    # an all-1.0 setup with a 1.0 catalyst must score exactly 1.0 (weights sum=1)
    q = calculate_setup_quality(
        gap_pct=1.0, relative_volume=10.0, structure_quality=1.0,
        above_vwap=True, opening_strength="strong", data_quality=1.0,
        catalyst_score=1.0,
    )
    assert q.score == pytest.approx(1.0)


def test_catalyst_score_clamped():
    q = calculate_setup_quality(**_KW, catalyst_score=5.0)
    assert q.components["catalyst"] == 1.0
