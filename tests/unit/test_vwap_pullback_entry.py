"""No-chase VWAP-pullback entry primitive: finds the reclaim bar, tight structure stop."""

import pandas as pd

from strategy.evaluation.vwap_pullback_entry import find_vwap_pullback_entry

COLS = ["high", "low", "close", "vwap"]


def _df(rows):
    return pd.DataFrame(rows, columns=COLS)


def test_finds_first_vwap_reclaim():
    # bar0 trades above VWAP (no touch), bar1 dips to VWAP and closes back over it -> entry there
    b = _df([
        (10.5, 10.2, 10.4, 10.0),     # above vwap all bar
        (10.3, 9.9, 10.2, 10.0),      # low 9.9 <= vwap 10.0 <= close 10.2 -> RECLAIM
        (10.6, 10.3, 10.5, 10.1),
    ])
    r = find_vwap_pullback_entry(b)
    assert r.found and r.entry_idx == 1
    assert r.entry_price == 10.2 and r.stop_price == 9.9      # close / low of the reclaim bar
    assert r.stop_price < r.entry_price                       # a tight structure stop


def test_no_reclaim_when_price_stays_below_vwap():
    # a one-way fade entirely under VWAP -> never reclaims -> no entry (anti falling-knife)
    b = _df([(9.9, 9.5, 9.6, 10.0), (9.6, 9.2, 9.3, 10.0), (9.3, 9.0, 9.1, 10.0)])
    r = find_vwap_pullback_entry(b)
    assert not r.found and "no vwap reclaim" in r.reason


def test_no_reclaim_when_price_stays_above_vwap():
    # price never pulls back to VWAP (no touch) -> no no-chase entry offered
    b = _df([(10.5, 10.2, 10.4, 10.0), (10.8, 10.5, 10.7, 10.1), (11.0, 10.7, 10.9, 10.2)])
    r = find_vwap_pullback_entry(b)
    assert not r.found


def test_empty_bars():
    assert not find_vwap_pullback_entry(_df([])).found
