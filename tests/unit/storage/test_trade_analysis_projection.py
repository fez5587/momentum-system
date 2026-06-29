"""Tests for the AI trade-analysis dashboard projection."""

import json
from datetime import date

import pytest

from storage.event_store import EventStore
from storage.projections import query_trade_analysis


@pytest.fixture
def store():
    s = EventStore(":memory:")
    yield s
    s.close()


SESS = date(2026, 6, 29)


def _seed(store, atype, symbol, decision, confidence, summary, concerns, sess=SESS):
    store.con.execute(
        "INSERT INTO ai_trade_analysis_cache (analysis_type, symbol, session_date, "
        "context_hash, decision, confidence, summary, concerns, detail, model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [atype, symbol, sess, "h", decision, confidence, summary,
         json.dumps(concerns), "{}", "test-model"],
    )


def test_empty_when_no_rows(store):
    assert query_trade_analysis(store) == {}


def test_groups_by_type_and_symbol(store):
    _seed(store, "armed", "AAA", "pursue", 0.8, "clean gap", ["thin float"])
    _seed(store, "weak", "BBB", "monitor", 0.4, "soft", [])
    _seed(store, "postmortem", "AAA", "none", 0.0, "stopped out", ["entered late"])
    _seed(store, "eod", "", "none", 0.0, "decent day", ["press winners"])

    ta = query_trade_analysis(store)
    assert ta["armed"]["AAA"]["decision"] == "pursue"
    assert ta["armed"]["AAA"]["concerns"] == ["thin float"]
    assert ta["weak"]["BBB"]["decision"] == "monitor"
    assert ta["postmortem"]["AAA"]["summary"] == "stopped out"
    # the EOD note lands under the '' symbol key
    assert ta["eod"][""]["summary"] == "decent day"


def test_scopes_to_latest_session(store):
    _seed(store, "armed", "OLD", "avoid", 0.5, "yesterday", [], sess=date(2026, 6, 26))
    _seed(store, "armed", "NEW", "pursue", 0.9, "today", [], sess=SESS)
    ta = query_trade_analysis(store)  # MAX(session_date) -> only the latest day
    assert "NEW" in ta["armed"] and "OLD" not in ta["armed"]


def test_for_date_selects_past_session(store):
    _seed(store, "armed", "OLD", "avoid", 0.5, "yesterday", [], sess=date(2026, 6, 26))
    _seed(store, "armed", "NEW", "pursue", 0.9, "today", [], sess=SESS)
    ta = query_trade_analysis(store, for_date="2026-06-26")
    assert "OLD" in ta["armed"] and "NEW" not in ta["armed"]
