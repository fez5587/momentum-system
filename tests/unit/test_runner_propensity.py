"""Runner-propensity selection primitive: transparent, frozen, monotone tiers."""

from strategy.evaluation.runner_propensity import runner_propensity


def test_big_gap_cheap_scores_highest():
    r = runner_propensity(1.50, premarket_gap_pct=0.80)   # < $2 and +80% gap
    assert r.tier == 5 and r.gap_tier == 3 and r.price_tier == 2
    assert r.gap_source == "premarket"


def test_non_gapper_scores_low():
    # a $40 name with a 2% gap is not a runner candidate
    r = runner_propensity(40.0, gap_pct=0.02)
    assert r.tier == 0 and r.gap_tier == 0 and r.price_tier == 0


def test_gap_dominates_price():
    # a big-gap rich name outscores a tiny-gap cheap name -> gap is the heavier axis
    big_gap_rich = runner_propensity(12.0, premarket_gap_pct=0.60)   # gap3 + price0 = 3
    small_gap_cheap = runner_propensity(1.0, premarket_gap_pct=0.12)  # gap1 + price2 = 3
    assert big_gap_rich.gap_tier > small_gap_cheap.gap_tier
    # equal total here, but the gap axis spans more -> a >=50% gap alone (3) beats any
    # price-only contribution (max 2)
    assert runner_propensity(50.0, premarket_gap_pct=0.55).tier >= 3


def test_monotonic_in_gap_and_cheapness():
    base = runner_propensity(3.0, premarket_gap_pct=0.20)
    assert runner_propensity(3.0, premarket_gap_pct=0.60).tier > base.tier   # bigger gap
    assert runner_propensity(1.0, premarket_gap_pct=0.20).tier > base.tier   # cheaper


def test_premarket_gap_preferred_over_rth():
    # when both are present the premarket gap is the one used
    r = runner_propensity(3.0, gap_pct=0.05, premarket_gap_pct=0.60)
    assert r.gap_source == "premarket" and r.gap_used == 0.60 and r.gap_tier == 3


def test_rth_gap_fallback_and_missing():
    assert runner_propensity(3.0, gap_pct=0.30).gap_source == "rth"
    assert runner_propensity(3.0).gap_source == "none"      # no gap data -> tier from price only
    assert runner_propensity(3.0).gap_tier == 0
