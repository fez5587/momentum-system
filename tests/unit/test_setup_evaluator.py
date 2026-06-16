"""Deterministic strategy evaluation tests (Milestone 1)."""

from datetime import datetime

from strategy.evaluation.setup_evaluator import evaluate_setup
from tests.synthetic import bull_flag_bars, fading_bars, tiny_bars

EVAL_TIME = datetime(2026, 6, 11, 9, 45)


def test_bull_flag_is_ready():
    result = evaluate_setup(
        bull_flag_bars(),
        previous_close=10.0,
        avg_daily_volume=500_000,
        evaluation_time=EVAL_TIME,
    )
    assert result.status == "ready"
    assert result.success_score_pct >= 60.0
    setup = result.setups[0]
    assert setup["entry_price"] > 0
    assert 0 < setup["stop_loss_price"] < setup["entry_price"]
    assert 0 <= setup["quality_score"] <= 1


def test_fading_tape_is_blocked():
    result = evaluate_setup(
        fading_bars(),
        previous_close=10.0,
        avg_daily_volume=500_000,
        evaluation_time=EVAL_TIME,
    )
    assert result.status == "blocked"
    assert result.success_score_pct < 60.0


def test_insufficient_data_is_blocked():
    result = evaluate_setup(tiny_bars(), previous_close=5.0, evaluation_time=EVAL_TIME)
    assert result.status == "blocked"
    assert "insufficient_data" in (result.reason or "")


def test_after_cutoff_is_late():
    result = evaluate_setup(
        bull_flag_bars(),
        previous_close=10.0,
        avg_daily_volume=500_000,
        evaluation_time=datetime(2026, 6, 11, 15, 30),
    )
    assert result.status == "late"


def test_criteria_results_cover_all_weights():
    result = evaluate_setup(
        bull_flag_bars(),
        previous_close=10.0,
        avg_daily_volume=500_000,
        evaluation_time=EVAL_TIME,
    )
    expected = {
        "sufficient_data", "gap", "relative_volume", "impulse", "pullback",
        "pullback_volume", "vwap", "candle_quality", "breakout",
    }
    evaluated = set(result.criteria_names_passed) | set(result.criteria_names_failed)
    assert expected <= evaluated
