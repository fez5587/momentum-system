"""Shared exit logic: simulate_exit (backtest) and manage_live must agree.

Locks in the rule behaviours we sweep and then run live: fixed target, move to
breakeven, trail under prior lows / by percent, scale-out, and first-red exit.
"""

import pandas as pd

from strategy.exits import (
    ExitConfig,
    TRAIL_PCT,
    TRAIL_PRIOR_LOW,
    manage_live,
    simulate_exit,
)


def _bars(seq):
    return pd.DataFrame([{"high": h, "low": lo, "close": c} for h, lo, c in seq])


# runs 10 -> 12.2, then collapses to 9.4 on the last bar
RUN_THEN_DROP = _bars([(10.5, 9.8, 10.4), (11.0, 10.2, 10.9),
                       (12.2, 11.5, 12.0), (12.0, 9.4, 9.6)])


def test_static_target_hits_2r():
    r = simulate_exit(10.0, 9.0, RUN_THEN_DROP, ExitConfig(target_r=2.0))
    assert r.reason == "target"
    assert round(r.r_multiple, 2) == 2.0


def test_breakeven_saves_a_runner_from_full_loss():
    # far target so it can't take profit; breakeven@1R moves stop to entry,
    # so the final collapse exits at ~0R instead of -1R
    r = simulate_exit(10.0, 9.0, RUN_THEN_DROP,
                      ExitConfig(target_r=5.0, breakeven_at_r=1.0))
    assert r.reason == "breakeven"
    assert abs(r.r_multiple) < 1e-6


def test_trail_prior_low_captures_partial_gain():
    r = simulate_exit(10.0, 9.0, RUN_THEN_DROP,
                      ExitConfig(target_r=5.0, trail_mode=TRAIL_PRIOR_LOW, trail_after_r=1.0))
    assert r.reason == "trail_stop"
    assert r.r_multiple > 1.0   # locked in more than breakeven


def test_scale_out_blends_realized_r():
    r = simulate_exit(10.0, 9.0, RUN_THEN_DROP,
                      ExitConfig(target_r=3.0, scale_out_r=1.0, scale_out_pct=0.5))
    # half booked at +1R, the rest rides to session end (close 9.6 = -0.4R)
    assert any(f.reason == "scale_out" for f in r.fills)
    assert round(r.r_multiple, 2) == round(0.5 * 1.0 + 0.5 * -0.4, 2)


def test_first_red_exits_on_break_of_prior_low():
    bars = _bars([(10.5, 10.1, 10.4), (11.0, 10.6, 10.9), (10.8, 9.9, 10.0)])
    # bar 3 closes 10.0 < bar 2 low 10.6 -> first red exit
    r = simulate_exit(10.0, 9.0, bars, ExitConfig(target_r=9.0, first_red_exit=True))
    assert r.reason == "first_red"


def test_stop_is_conservative_intrabar():
    # a bar whose range spans BOTH stop and target -> assume stop fills first
    bars = _bars([(12.5, 8.9, 10.0)])
    r = simulate_exit(10.0, 9.0, bars, ExitConfig(target_r=2.0))
    assert r.reason == "stop_loss"
    assert round(r.r_multiple, 2) == -1.0


def test_manage_live_ratchets_stop_to_breakeven():
    bars = _bars([(11.2, 10.1, 11.0)])  # reached +1.2R
    d = manage_live(10.0, 9.0, bars, ExitConfig(breakeven_at_r=1.0))
    assert d.desired_stop >= 10.0       # moved up to (at least) entry


def test_manage_live_pct_trail():
    bars = _bars([(12.0, 10.5, 11.8)])  # high-water 12
    d = manage_live(10.0, 9.0, bars,
                    ExitConfig(trail_mode=TRAIL_PCT, trail_pct=0.05, trail_after_r=1.0))
    assert round(d.desired_stop, 2) == round(12.0 * 0.95, 2)
