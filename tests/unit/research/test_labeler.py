"""Labeler core: strict time-split + correct labels on synthetic sessions."""

from datetime import date, datetime

import pandas as pd

from research.labeler import compute_setup, compute_vwap_reclaim_setups

COLS = ["timestamp", "session_date", "is_premarket", "is_regular_hours",
        "is_afterhours", "open", "high", "low", "close", "volume", "vwap"]
D = date(2026, 6, 1)


def _bars(seq):
    """seq = list of (minute_offset_from_0930, open, high, low, close)."""
    rows = []
    for m, o, h, lo, c in seq:
        ts = datetime(2026, 6, 1, 9, 30) + pd.Timedelta(minutes=m)
        rows.append([ts, D, False, True, False, o, h, lo, c, 1000, c])
    return pd.DataFrame(rows, columns=COLS)


def _runner_bars():
    # ORB (bars 0-4): high 1.10 / low 1.00. Then runs 1.10 -> 2.00.
    orb = [(0, 1.00, 1.05, 1.00, 1.04), (1, 1.04, 1.08, 1.02, 1.06),
           (2, 1.06, 1.10, 1.04, 1.09), (3, 1.09, 1.10, 1.05, 1.08),
           (4, 1.08, 1.10, 1.06, 1.09)]
    run = [(5, 1.10, 1.30, 1.09, 1.28), (6, 1.28, 1.60, 1.25, 1.55),
           (7, 1.55, 2.00, 1.50, 1.95), (8, 1.95, 2.00, 1.80, 1.90),
           (9, 1.90, 1.95, 1.70, 1.75)]
    return _bars(orb + run)


def test_runner_session_labels_and_timesplit():
    setup, feat, label = compute_setup("RUN", D, _runner_bars(),
                                       prior_close=0.80, avg_vol=50_000, cats={})
    # geometry: entry = ORB high 1.10, invalidation = ORB low 1.00
    assert setup["entry_reference_price"] == 1.10
    assert setup["invalidation_price"] == 1.00
    # gap% off prior close 0.80, open 1.00 -> +25%
    assert abs(feat["gap_pct"] - 0.25) < 1e-6
    # TIME SPLIT: the feature VWAP must reflect ONLY the ORB bars (~1.0x),
    # NOT the post-setup run to 2.00 — proves no look-ahead.
    assert feat["vwap"] < 1.15
    # forward labels see the run
    assert label["max_upside_next_15m"] > 0.5            # ran > +50% off entry
    assert label["reached_1r_before_minus_1r"] is True   # +1R (1.20) hit
    assert label["reached_2r_before_minus_1r"] is True   # +2R (1.30) hit
    assert label["failed_breakout_flag"] is False        # never lost the ORB low
    assert label["trend_day_flag"] is True               # HoD 2.00 vs open 1.00 = +100%


def test_fader_session_labels():
    orb = [(0, 1.00, 1.05, 1.00, 1.04), (1, 1.04, 1.08, 1.02, 1.06),
           (2, 1.06, 1.10, 1.04, 1.09), (3, 1.09, 1.10, 1.05, 1.08),
           (4, 1.08, 1.10, 1.06, 1.09)]
    fade = [(5, 1.10, 1.12, 1.05, 1.06), (6, 1.06, 1.07, 0.95, 0.96),
            (7, 0.96, 0.98, 0.85, 0.88), (8, 0.88, 0.90, 0.84, 0.86),
            (9, 0.86, 0.88, 0.82, 0.83)]
    setup, feat, label = compute_setup("FADE", D, _bars(orb + fade),
                                       prior_close=0.90, avg_vol=50_000, cats={})
    assert label["failed_breakout_flag"] is True         # poked >1.10 then lost 1.00
    assert label["reached_1r_before_minus_1r"] is False  # stopped before +1R
    assert label["trend_day_flag"] is False              # HoD 1.12 vs open 1.00 = +12%
    assert label["max_drawdown_next_15m"] < 0            # went underwater


def test_skips_session_without_full_opening_range():
    # only 3 RTH bars -> no opening range -> no setup
    short = _bars([(0, 1.0, 1.1, 1.0, 1.05), (1, 1.05, 1.1, 1.0, 1.06),
                   (2, 1.06, 1.1, 1.0, 1.04)])
    assert compute_setup("X", D, short, prior_close=1.0, avg_vol=1000, cats={}) is None


# --- vwap_reclaim shadow track --------------------------------------------

def _leg(m0, p0, p1, n):
    """n bars (minute offsets from m0) walking linearly p0->p1 with small wicks."""
    out, span = [], abs(p1 - p0) / n
    for i in range(n):
        o = p0 + (p1 - p0) * i / n
        c = p0 + (p1 - p0) * (i + 1) / n
        out.append((m0 + i, o, max(o, c) + span * 0.3 + 0.005, min(o, c) - span * 0.3 - 0.005, c))
    return out


def test_vwap_reclaim_fires_and_labels_a_winner():
    # impulse 1.00->1.40, pullback to 1.25 (holds), curl 1.25->1.36 (new high, below the
    # 1.40 leg high), then a continuation to 1.60 -> the forward label should be a +1R win.
    seq = (_leg(0, 1.00, 1.40, 12) + _leg(12, 1.40, 1.25, 6)
           + _leg(18, 1.25, 1.36, 6) + _leg(24, 1.36, 1.60, 8))
    setups = compute_vwap_reclaim_setups("CURL", D, _bars(seq), prior_close=0.80,
                                         avg_vol=50_000, cats={})
    assert setups, "detector should fire on the curl"
    setup, label = setups[0]
    assert setup["setup_name"] == "vwap_reclaim" and setup["setup_version"] == "vr1"
    assert 1.25 < setup["entry_reference_price"] < 1.40       # bought the curl, below the leg high
    assert setup["invalidation_price"] < setup["entry_reference_price"]  # stop = pullback low
    assert setup["session_minute_number"] > 17               # fired after the pullback
    assert label["reached_1r_before_minus_1r"] is True       # the continuation paid out
    assert label["failed_breakout_flag"] is False


def test_vwap_reclaim_silent_on_one_way_fade():
    # a monotone fade below VWAP -> no curl -> no shadow setups (anti falling-knife)
    fade = _bars(_leg(0, 2.00, 1.20, 26))
    assert compute_vwap_reclaim_setups("FADE", D, fade, prior_close=2.10,
                                       avg_vol=50_000, cats={}) == []
