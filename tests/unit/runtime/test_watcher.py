"""Watcher state machine tests (Milestone 4)."""

from datetime import date

import pandas as pd
import pytest

from runtime.watcher import WatchCandidate, Watcher, WatcherConfig
from storage.event_schema import EventMode
from storage.event_store import EventStore
from tests.synthetic import SESSION_DATE, bull_flag_bars, fading_bars, tiny_bars


class FakeProvider:
    def __init__(self):
        self.candidates: list[WatchCandidate] = []
        self.bars: dict[str, pd.DataFrame] = {}

    def get_candidates(self, session_date: date):
        return self.candidates

    def get_bars(self, symbol: str, session_date: date):
        return self.bars.get(symbol, pd.DataFrame())


@pytest.fixture
def store():
    s = EventStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def provider():
    p = FakeProvider()
    p.candidates = [
        WatchCandidate("GOOD", previous_close=10.0, avg_daily_volume=500_000),
        WatchCandidate("FADE", previous_close=10.0, avg_daily_volume=500_000),
        WatchCandidate("TINY", previous_close=5.0),
    ]
    p.bars = {
        "GOOD": bull_flag_bars("GOOD"),
        "FADE": fading_bars("FADE"),
        "TINY": tiny_bars("TINY"),
    }
    return p


def make_watcher(store, provider, **kw):
    config = WatcherConfig(
        session_id="test-session", mode=EventMode.PAPER, min_quality=0.0, **kw
    )
    return Watcher(store, provider, config)


def test_tick_classifies_ready_blocked(store, provider):
    watcher = make_watcher(store, provider)
    result = watcher.tick(SESSION_DATE)
    # TINY has < min_bars, so it stays "watching" and is not evaluated
    assert result.evaluated == 2
    assert "GOOD" in result.ready
    assert "FADE" in result.blocked
    assert "TINY" not in result.ready and "TINY" not in result.blocked
    assert set(result.discovered) == {"GOOD", "FADE", "TINY"}


def test_tick_emits_canonical_events(store, provider):
    make_watcher(store, provider).tick(SESSION_DATE)
    assert len(store.query_events(event_type="symbol_discovered")) == 3
    assert len(store.query_events(event_type="signal_ready", symbol="GOOD")) == 1
    assert len(store.query_events(event_type="criteria_evaluated")) == 2
    assert len(store.query_events(event_type="signal_blocked")) >= 1
    states = store.query_events(event_type="symbol_state_changed", symbol="GOOD")
    assert states  # discovered -> ... -> ready transitions recorded


def test_signal_ready_debounced_across_ticks(store, provider):
    watcher = make_watcher(store, provider)
    watcher.tick(SESSION_DATE)
    watcher.tick(SESSION_DATE)
    # ready emitted once even though GOOD stays ready
    assert len(store.query_events(event_type="signal_ready", symbol="GOOD")) == 1
    # discovery also only once
    assert len(store.query_events(event_type="symbol_discovered", symbol="GOOD")) == 1


def test_max_symbols_cap(store, provider):
    watcher = make_watcher(store, provider, max_symbols=1)
    result = watcher.tick(SESSION_DATE)
    assert result.evaluated == 1


def test_provider_failure_is_caught(store):
    class Broken:
        def get_candidates(self, session_date):
            return [WatchCandidate("BOOM")]

        def get_bars(self, symbol, session_date):
            raise RuntimeError("data source down")

    result = make_watcher(store, Broken()).tick(SESSION_DATE)
    assert result.errors
    assert result.ready == []
