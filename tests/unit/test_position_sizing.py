"""Account-aware position sizing: risk-based, capped by buying power + liquidity.

Critical for a small REAL account ($300) — sizing must come off real equity and
no single position can exceed buying power.
"""

from strategy.risk.position_sizing import PositionSizingConfig, calculate_position_size

CFG = PositionSizingConfig(risk_per_trade_pct=0.01)


def test_sizes_off_small_equity():
    # $300 equity, 1% = $3 risk; entry 4.00 stop 3.80 (risk 0.20) -> 14 shares
    # (3/0.20 = 14.999.. truncates down — conservative, never over-risk)
    r = calculate_position_size(4.0, 3.80, equity=300, config=CFG)
    assert r.position_size == 14
    assert r.dollar_amount <= 300 * CFG.risk_per_trade_pct / 0.20 * 4.0 + 1e-6


def test_value_cap_binds_on_tight_stop():
    # tight stop -> risk-based wants 150 sh ($600); cap at $120 -> 30 shares
    r = calculate_position_size(4.0, 3.98, equity=300, config=CFG, max_position_value=120)
    assert r.position_size == 30
    assert r.dollar_amount <= 120 + 1e-6


def test_liquidity_cap_binds():
    r = calculate_position_size(4.0, 3.80, equity=300, config=CFG, max_shares=5)
    assert r.position_size == 5


def test_default_equity_when_unset():
    # no equity passed -> falls back to config default (not zero)
    r = calculate_position_size(4.0, 3.80, config=PositionSizingConfig(default_equity=100000))
    assert r.position_size > 0
