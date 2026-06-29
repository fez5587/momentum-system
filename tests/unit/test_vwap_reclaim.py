"""VWAP-reclaim continuation detector (P0): binary gates on synthetic first-pullbacks.

Pure + deterministic. A real-trader-name regression fixture (MIMI/SDOT) is deferred
to the P2 nightly scorer over real shadow signals."""

import pandas as pd

from strategy.evaluation.vwap_reclaim import detect_vwap_reclaim

COLS = ["open", "high", "low", "close", "volume"]


def _seg(p0, p1, n, vol=10000):
    """n bars walking linearly from price p0 to p1 (small symmetric wicks)."""
    rows, span = [], abs(p1 - p0) / n
    for i in range(n):
        o = p0 + (p1 - p0) * i / n
        c = p0 + (p1 - p0) * (i + 1) / n
        rows.append((o, max(o, c) + span * 0.3 + 0.01, min(o, c) - span * 0.3 - 0.01, c, vol))
    return rows


def _df(rows):
    return pd.DataFrame(rows, columns=COLS)


# impulse 4.0->6.0, pullback 6.0->5.2 (holds 50% of the run, =5.0), curl 5.2->5.7
# (a new high above the pullback, still below the 6.0 HOD).
CLEAN = _df(_seg(4.0, 6.0, 18, 12000) + _seg(6.0, 5.2, 8, 8000) + _seg(5.2, 5.7, 14, 11000))


def test_clean_vwap_reclaim_fires():
    r = detect_vwap_reclaim(CLEAN)
    assert r.is_valid and r.reason == "vwap_reclaim"
    assert r.target_level == round(CLEAN["high"].max(), 4)        # target = HOD
    assert 5.0 < r.stop_level < 5.5                               # stop = pullback low
    assert r.breakout_level < r.target_level                      # entry below HOD (the curl)
    sv = r.signal_values
    assert sv["pullback_held_frac"] >= 0.5 and sv["ema9_ge_ema20"]
    assert sv["dist_from_vwap"] >= 0                              # at/above VWAP


def test_at_hod_rejected():
    # a pure run-up ending at the high -> no pullback to curl from (anti spike-top)
    r = detect_vwap_reclaim(_df(_seg(4.0, 6.0, 30, 11000)))
    assert not r.is_valid and "HOD" in (r.reason or "")


def test_deep_pullback_rejected():
    # pullback to 4.7 < the 50% hold level (5.0) -> gave the move back
    deep = _df(_seg(4.0, 6.0, 18, 12000) + _seg(6.0, 4.7, 8, 8000) + _seg(4.7, 5.4, 14, 11000))
    r = detect_vwap_reclaim(deep)
    assert not r.is_valid and "hold" in (r.reason or "").lower()


def test_no_impulse_rejected():
    # flat chop, never ran -> no impulse
    flat = _df([(4.0, 4.03, 3.97, 4.0, 9000) for _ in range(30)])
    r = detect_vwap_reclaim(flat)
    assert not r.is_valid


def test_insufficient_bars():
    r = detect_vwap_reclaim(_df(_seg(4.0, 6.0, 10)))
    assert not r.is_valid and "need" in (r.reason or "")


def test_target_rr_and_macd_tag_logged():
    r = detect_vwap_reclaim(CLEAN)
    assert "target_rr" in r.signal_values and "macd_hist" in r.signal_values
    assert r.signal_values["target_rr"] > 0                      # HOD is above the entry


def test_unplaceable_tight_stop_rejected():
    """P3: a very shallow pullback yields a sub-1.5% R (stop inside the spread). The
    curl is otherwise valid, so the placeability gate must reject it -- and disabling
    the gate must let it fire (proving it's the gate, not another criterion)."""
    # impulse 4.0->6.0, SHALLOW pullback to ~5.80 (held ~90%), curl to ~5.84:
    # entry ~5.84, stop ~5.79 -> R ~0.9% of price -> unplaceable.
    tight = _df(_seg(4.0, 6.0, 18, 12000) + _seg(6.0, 5.80, 8, 8000) + _seg(5.80, 5.84, 14, 11000))
    r = detect_vwap_reclaim(tight)
    assert not r.is_valid and "unplaceable" in (r.reason or "")
    assert r.signal_values["r_frac"] < 0.015                     # the diagnosed cause
    off = detect_vwap_reclaim(tight, min_r_frac=0.0, min_r_abs=0.0)
    assert off.is_valid and off.reason == "vwap_reclaim"         # gate was the only blocker


def test_clean_fire_is_placeable():
    r = detect_vwap_reclaim(CLEAN)
    assert r.is_valid and r.signal_values["r_frac"] >= 0.015     # a real, placeable stop


def test_local_anchor_rearms_after_higher_leg_gave_back():
    """P1: the whole-session anchor goes permanently blind once the day's single HOD
    deeply gives back, but a name often sets a SECOND leg from a higher base and curls
    off it. leg1 spikes to 7 (low vol so VWAP isn't dragged up) then gives most of it
    back; leg2 runs 4.5->5.8 and curls off a higher-low pullback. Global anchor: the
    leg1 give-back reads as 'broke the hold' forever. Local anchor: re-arms on leg2."""
    data = (_seg(4.0, 7.0, 12, 3000) + _seg(7.0, 4.5, 10, 3000) + _seg(4.5, 5.8, 10, 15000)
            + _seg(5.8, 5.3, 6, 9000) + _seg(5.3, 5.6, 8, 14000))
    df = _df(data)
    g = detect_vwap_reclaim(df, lookback=None)                   # legacy whole-session anchor
    l = detect_vwap_reclaim(df, lookback=25)                     # local-swing anchor
    assert not g.is_valid and "hold" in (g.reason or "").lower()  # blocked by leg1's give-back
    assert l.is_valid and l.reason == "vwap_reclaim"             # re-armed on leg2
    assert l.target_level < 6.0                                  # targets leg2's local high, not the 7.0 HOD
    assert l.signal_values["pullback_held_frac"] >= 0.5          # the higher low held locally
