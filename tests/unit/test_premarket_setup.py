"""Premarket setup detector — fires on a gapped, liquid, VWAP-holding premarket breakout;
silent on thin/no-gap/below-VWAP premarket noise."""

import pandas as pd

from strategy.evaluation.premarket_setup import detect_premarket_setup


def _pm_run(n=14, start=3.0, step=0.06, vol=20_000):
    rows = []
    px = start
    for i in range(n):
        o = px
        px = px + step
        rows.append((o, px + 0.01, o - 0.01, px, vol))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def test_fires_on_gapped_liquid_premarket_breakout():
    sig = detect_premarket_setup(_pm_run(), prev_close=2.5)   # gap to ~3.8 off 2.5 = ~+50%
    assert sig.is_valid, sig.reason
    assert sig.reason == "premarket_setup"
    assert sig.entry_level and sig.stop_level


def test_no_gap_does_not_fire():
    sig = detect_premarket_setup(_pm_run(), prev_close=100.0)  # tiny gap off a high prev_close
    assert not sig.is_valid and sig.reason == "gap below floor"


def test_thin_premarket_volume_does_not_fire():
    sig = detect_premarket_setup(_pm_run(vol=500), prev_close=2.5)  # gapped but no participation
    assert not sig.is_valid and sig.reason == "thin premarket volume"


def test_below_price_floor_does_not_fire():
    sig = detect_premarket_setup(_pm_run(start=0.5, step=0.01), prev_close=0.4,
                                 min_pm_volume=0)
    assert not sig.is_valid and sig.reason == "below price floor"


def test_knife_down_off_the_high_does_not_fire():
    b = _pm_run()
    b.loc[b.index[-1], ["close", "high", "low"]] = [3.1, 3.15, 3.05]  # dumps off the pm high / VWAP
    sig = detect_premarket_setup(b, prev_close=2.5)
    # fails a continuation gate (velocity, breakout, or VWAP) — no longer a valid setup
    assert not sig.is_valid
    assert sig.reason in ("no premarket velocity", "not at premarket high", "below premarket VWAP")


def test_insufficient_bars_is_quiet():
    sig = detect_premarket_setup(_pm_run(n=5), prev_close=2.5)
    assert not sig.is_valid and "need >=" in (sig.reason or "")
