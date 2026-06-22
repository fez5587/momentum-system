"""Shared exit logic: simulate_exit (backtest) and manage_live must agree.

Locks in the rule behaviours we sweep and then run live: fixed target, move to
breakeven, trail under prior lows / by percent, scale-out, and first-red exit.
"""

import pandas as pd

from strategy.exits import (
    ExitConfig,
    TRAIL_PCT,
    TRAIL_PRIOR_LOW,
    catastrophe_triggered,
    manage_live,
    parse_profit_tiers,
    simulate_exit,
)


def test_parse_profit_tiers():
    assert parse_profit_tiers("8:3,15:9") == [(0.08, 0.03), (0.15, 0.09)]
    assert parse_profit_tiers("") == []


def test_catastrophe_pct_arm():
    # 10% pct floor, no known stop -> the only thing protecting a naked position
    assert catastrophe_triggered(1.00, 0.89, None, 0.10, 1.5) is True   # -11%, fires
    assert catastrophe_triggered(1.00, 0.92, None, 0.10, 1.5) is False  # -8%, safe
    # NIVF: entry 0.9936 ran to 0.76 (-23.4%) with NO stop -> would have fired
    assert catastrophe_triggered(0.9936, 0.7608, None, 0.10, 1.5) is True


def test_catastrophe_risk_arm_fires_before_pct():
    # tight 5% stop: -1.5R = -7.5% < the 10% pct floor, so the risk arm catches it sooner
    assert catastrophe_triggered(1.00, 0.92, 0.95, 0.10, 1.5) is True    # past -1.5R (-1.6R)
    assert catastrophe_triggered(1.00, 0.93, 0.95, 0.10, 1.5) is False   # -1.4R, not yet
    # a stop at/above entry is not a usable risk reference -> only the pct arm applies
    assert catastrophe_triggered(1.00, 0.94, 1.00, 0.10, 1.5) is False


def test_catastrophe_off_and_in_profit():
    assert catastrophe_triggered(1.00, 0.50, None, 0.0, 1.5) is False    # pct arm disabled, no stop
    assert catastrophe_triggered(1.00, 1.20, 0.95, 0.10, 1.5) is False   # in profit
    assert catastrophe_triggered(1.00, 0.0, None, 0.10, 1.5) is False    # bad price -> no-op


def test_profit_lock_keeps_minimum_gain():
    # entry 10, stop 9. price runs to +12% (11.2) then fades; +8%->lock+3% tier
    # pins the stop to 10.30, so the fade exits in profit instead of at the stop.
    bars = _bars([(10.5, 9.9, 10.4), (11.2, 10.6, 11.0), (10.4, 9.5, 9.6)])
    cfg = ExitConfig(target_r=20.0, profit_lock_tiers=[(0.08, 0.03)])
    r = simulate_exit(10.0, 9.0, bars, cfg)
    assert r.fills[-1].price >= 10.30 - 1e-6   # sold at/above the +3% lock
    assert r.r_multiple > 0                    # a locked-in WIN, not a loss


def test_profit_lock_live_desired_stop():
    bars = _bars([(11.2, 10.6, 11.0)])       # high-water +12%
    d = manage_live(10.0, 9.0, bars, ExitConfig(profit_lock_tiers=[(0.08, 0.03), (0.15, 0.09)]))
    assert round(d.desired_stop, 2) == 10.30  # +3% locked (only +8% tier reached)


def test_pct_breakeven_live_moves_stop_to_entry():
    # entry 10, stop 9; once price tags +5% (10.5) the % breakeven pins stop to entry
    d = manage_live(10.0, 9.0, _bars([(10.5, 10.1, 10.4)]), ExitConfig(breakeven_at_pct=0.05))
    assert round(d.desired_stop, 2) == 10.0


def test_pct_breakeven_not_reached_holds_initial_stop():
    # only +3% high-water, under the 5% threshold -> stop stays at the initial 9.0
    d = manage_live(10.0, 9.0, _bars([(10.3, 10.0, 10.2)]), ExitConfig(breakeven_at_pct=0.05))
    assert round(d.desired_stop, 2) == 9.0


def test_pct_breakeven_saves_a_reversing_winner():
    # pops +6% then collapses below the original stop; the % breakeven turns a
    # -1R loss into a 0R scratch (the "winner round-trips to a loss" failure)
    bars = _bars([(10.6, 10.2, 10.5), (10.1, 8.5, 8.6)])
    cfg = ExitConfig(target_r=20.0, breakeven_at_pct=0.05)
    r = simulate_exit(10.0, 9.0, bars, cfg)
    assert r.fills[-1].price >= 10.0 - 1e-6   # exited at breakeven, not the 9.0 stop
    assert r.reason == "breakeven"
    assert abs(r.r_multiple) < 1e-6           # scratch, not a loss


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
