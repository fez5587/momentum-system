"""Momentum-ignition detector (P0): binary gates on synthetic vertical squeezes.

Pure + deterministic. A real-PLSM regression fixture is deferred to P2 (when the
nightly scorer runs over real shadow signals)."""

import pandas as pd

from strategy.evaluation.ignition import _last_higher_low, detect_momentum_ignition

COLS = ["open", "high", "low", "close", "volume"]

# a clean vertical ignition: 10 bars, all green, rising, last bar a new HOD on a
# big volume burst. open, high, low, close, volume.
IGNITION = [
    (4.00, 4.20, 3.90, 4.10, 5000),
    (4.10, 4.40, 4.05, 4.35, 6000),
    (4.35, 4.70, 4.30, 4.65, 7000),
    (4.65, 5.00, 4.60, 4.95, 8000),
    (4.95, 5.40, 4.90, 5.35, 9000),
    (5.35, 5.90, 5.30, 5.85, 10000),
    (5.85, 6.50, 5.80, 6.45, 12000),
    (6.45, 7.20, 6.40, 7.10, 14000),
    (7.10, 8.00, 7.05, 7.90, 16000),
    (7.90, 9.20, 7.85, 9.10, 60000),   # new HOD 9.20, volume burst 60k vs ~12k mean
]


def _df(rows):
    return pd.DataFrame(rows, columns=COLS)


def test_ignition_fires_with_correct_trigger_and_stop():
    r = detect_momentum_ignition(_df(IGNITION), prior_ath=5.0)
    assert r.is_valid and r.reason == "momentum_ignition"
    assert r.breakout_level == 9.20                 # the fresh HOD = entry reference
    assert r.stop_level == 7.05                     # last higher low (bar 8 low > bar 7 low)
    sv = r.signal_values
    assert sv["is_blue_sky"] and sv["new_hod"]
    assert sv["velocity_pct"] > 0.08
    assert sv["volume_burst_ratio"] >= 3.0
    assert sv["cum_volume"] >= 100_000


def test_not_blue_sky_blocks():
    # prior ATH above the session high -> not blue sky, even though it's a new HOD
    r = detect_momentum_ignition(_df(IGNITION), prior_ath=100.0)
    assert not r.is_valid and r.reason == "not blue-sky fresh-HOD"


def test_not_new_hod_blocks():
    # final bar fades below the session high (bar 8 = 8.00) -> no fresh HOD
    fade = IGNITION[:-1] + [(7.90, 7.95, 7.40, 7.50, 60000)]
    r = detect_momentum_ignition(_df(fade), prior_ath=5.0)
    assert not r.is_valid and r.reason == "not blue-sky fresh-HOD"
    assert r.signal_values["new_hod"] is False


def test_velocity_gate_blocks():
    # blue-sky + volume pass, but require an impossibly steep move -> velocity fails
    r = detect_momentum_ignition(_df(IGNITION), prior_ath=5.0, velocity_min=2.0)
    assert not r.is_valid and r.reason == "velocity gate failed"


def test_volume_burst_gate_blocks():
    # blue-sky + velocity pass, but require an impossible burst -> volume fails
    r = detect_momentum_ignition(_df(IGNITION), prior_ath=5.0, burst_mult=100.0)
    assert not r.is_valid and r.reason == "volume-burst gate failed"


def test_insufficient_bars():
    r = detect_momentum_ignition(_df(IGNITION[:4]), prior_ath=5.0)
    assert not r.is_valid and "need" in (r.reason or "")


def test_missing_prior_ath_is_not_blue_sky():
    # no prior ATH -> can't confirm blue sky -> blocked (fail-safe, never fires blind)
    r = detect_momentum_ignition(_df(IGNITION), prior_ath=None)
    assert not r.is_valid and r.reason == "not blue-sky fresh-HOD"


def test_last_higher_low_helper():
    # rising lows -> last established higher low beneath the final bar
    assert _last_higher_low([3.9, 4.05, 4.3, 4.6]) == 4.3   # excludes final bar (4.6)
    # no higher low -> fallback to the window min
    assert _last_higher_low([5.0, 4.0, 3.0, 2.0]) == 2.0
