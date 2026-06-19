"""Halt-gap decision edge cases (the riskiest heuristic in the entry guards)."""

from runtime.halt_guard import is_halt_gap

GAP = 180.0  # 3 min


def test_symbol_silent_while_book_healthy_is_a_halt():
    # this name's freshest bar is 5 min old; others printed 30s ago -> halted
    assert is_halt_gap(sym_age_s=300, global_age_s=30, gap_seconds=GAP) is True


def test_symbol_fresh_is_not_a_halt():
    assert is_halt_gap(sym_age_s=60, global_age_s=30, gap_seconds=GAP) is False


def test_feed_wide_lag_does_not_flag_the_book():
    # this name is stale AND so is the whole book -> ingest lag, not a halt
    assert is_halt_gap(sym_age_s=300, global_age_s=300, gap_seconds=GAP) is False


def test_no_bars_for_symbol_is_inert_not_a_halt():
    # None age (query glitch / session-date mismatch / no prints) fails SAFE:
    # the guard does nothing rather than mass-blocking entries
    assert is_halt_gap(sym_age_s=None, global_age_s=30, gap_seconds=GAP) is False


def test_no_global_data_is_inert():
    assert is_halt_gap(sym_age_s=300, global_age_s=None, gap_seconds=GAP) is False


def test_gap_zero_disables():
    assert is_halt_gap(sym_age_s=999, global_age_s=10, gap_seconds=0) is False


def test_boundary_exactly_at_gap_is_not_halt():
    # at exactly the threshold the symbol is still "fresh enough"
    assert is_halt_gap(sym_age_s=180, global_age_s=30, gap_seconds=GAP) is False
    assert is_halt_gap(sym_age_s=181, global_age_s=30, gap_seconds=GAP) is True
