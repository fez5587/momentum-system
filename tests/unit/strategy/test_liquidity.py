"""Bid/ask spread metric tests."""

import math

from strategy.evaluation.liquidity import compute_spread_pct


def test_normal_spread_is_fraction_of_mid():
    # bid 9.99 / ask 10.01 -> 0.02 / 10.0 mid = 0.002 (20 bps)
    assert math.isclose(compute_spread_pct(9.99, 10.01), 0.002, rel_tol=1e-9)


def test_wide_spread_on_thin_name():
    # bid 4.00 / ask 4.40 -> 0.40 / 4.20 mid ~= 0.0952 (a 9.5% spread)
    assert math.isclose(compute_spread_pct(4.00, 4.40), 0.40 / 4.20, rel_tol=1e-9)


def test_locked_book_is_zero_not_none():
    # bid == ask is a real, valid tight market, distinct from a missing quote
    assert compute_spread_pct(5.0, 5.0) == 0.0


def test_missing_side_is_none():
    assert compute_spread_pct(None, 10.0) is None
    assert compute_spread_pct(10.0, None) is None
    assert compute_spread_pct(None, None) is None


def test_crossed_book_is_none():
    # ask < bid is a bad/stale quote, not a negative spread
    assert compute_spread_pct(10.01, 9.99) is None


def test_non_positive_prices_are_none():
    assert compute_spread_pct(0.0, 1.0) is None
    assert compute_spread_pct(1.0, 0.0) is None
    assert compute_spread_pct(-1.0, 1.0) is None


def test_unparseable_inputs_are_none():
    assert compute_spread_pct("n/a", 10.0) is None
    assert compute_spread_pct(10.0, "n/a") is None
