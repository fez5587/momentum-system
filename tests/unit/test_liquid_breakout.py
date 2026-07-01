"""Liquid intraday-RVOL breakout detector — fires on a fillable intraday igniter,
stays SILENT on illiquid dust (the failure mode the naive runner detector had)."""

import pandas as pd

from strategy.evaluation.liquid_breakout import detect_liquid_intraday_breakout


def _rising(n=20, start=10.0, step=0.1, vol_early=1_000, vol_burst=200_000, burst=5):
    rows = []
    px = start
    for i in range(n):
        o = px
        px = px + step
        vol = vol_burst if i >= n - burst else vol_early
        rows.append((o, px + 0.01, o - 0.01, px, vol))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def test_fires_on_liquid_intraday_breakout():
    sig = detect_liquid_intraday_breakout(_rising())
    assert sig.is_valid, sig.reason
    assert sig.reason == "liquid_intraday_breakout"
    assert sig.entry_level and sig.stop_level


def test_illiquid_dust_does_not_fire():
    # AMCI/CCTG-class: same shape, but nobody's trading it -> tiny dollar-volume
    dust = _rising(vol_early=40, vol_burst=60)
    sig = detect_liquid_intraday_breakout(dust)
    assert not sig.is_valid
    assert sig.reason == "illiquid (dollar-volume floor)"


def test_below_price_floor_does_not_fire():
    cheap = _rising(start=0.8, step=0.02)   # penny dust, even if liquid
    sig = detect_liquid_intraday_breakout(cheap, min_dollar_vol=0, min_bar_dollar_vol=0)
    assert not sig.is_valid and sig.reason == "below price floor"


def test_rvol_recorded_not_gated_by_default():
    # steady-liquid grinder (flat volume, no within-session surge) STILL fires by default:
    # rvol is a recorded feature, not a hard gate (the MSTR-class fix). Passing rvol_min>0
    # re-enables the gate for callers who want it.
    flat = _rising(vol_early=200_000, vol_burst=200_000)
    sig = detect_liquid_intraday_breakout(flat)
    assert sig.is_valid                                   # fires by default
    assert sig.signal_values["rvol"] > 0                  # rvol still recorded
    gated = detect_liquid_intraday_breakout(flat, rvol_min=3.0)
    assert not gated.is_valid and gated.reason == "no rvol surge"


def test_not_at_session_high_does_not_fire():
    b = _rising()
    b.loc[b.index[-1], ["close", "high"]] = [10.5, 10.55]   # pulled back off the high
    sig = detect_liquid_intraday_breakout(b)
    assert not sig.is_valid and sig.reason in ("not at session high", "below session VWAP")


def test_insufficient_bars_is_quiet():
    sig = detect_liquid_intraday_breakout(_rising(n=6))
    assert not sig.is_valid and "need >=" in (sig.reason or "")
