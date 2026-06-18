"""Board/journal P&L header: the broker-authoritative split must reconcile.

The header shows `day P&L (matched M · open O)`. M is broker-authoritative
realized (day P&L minus current open unrealized) so M + O == day P&L exactly,
even though the per-trade rows use FIFO lot accounting (whose sum can differ on
partially-closed names). When the broker day P&L is unavailable, M falls back
to the FIFO realized sum.
"""

from momentum_cli import _matched_realized


def test_matched_plus_open_equals_day_pnl():
    day_pnl, open_unreal, fifo = -652.0, -339.0, -1339.0
    matched = _matched_realized(day_pnl, open_unreal, fifo)
    assert matched == day_pnl - open_unreal          # broker-authoritative
    assert abs((matched + open_unreal) - day_pnl) < 1e-9  # reconciles exactly
    assert matched != fifo                            # not the FIFO sum


def test_matched_reconciles_for_a_profitable_day():
    day_pnl, open_unreal, fifo = 1200.0, 800.0, 50.0
    matched = _matched_realized(day_pnl, open_unreal, fifo)
    assert matched == 400.0 and matched + open_unreal == day_pnl


def test_matched_falls_back_to_fifo_when_day_pnl_missing():
    # no broker equity snapshot -> use the FIFO realized sum
    assert _matched_realized(None, -339.0, -1339.0) == -1339.0
