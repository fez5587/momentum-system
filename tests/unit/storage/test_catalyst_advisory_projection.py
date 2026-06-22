"""Tests for the catalyst-advisory dashboard projection."""

import pytest

from storage.event_store import EventStore
from storage.projections import query_catalyst_advisory, query_catalyst_feed


@pytest.fixture
def store():
    s = EventStore(":memory:")
    yield s
    s.close()


def _seed(store, headline_hash, symbol, ctype, sentiment, conviction, dilutive):
    store.con.execute(
        "INSERT INTO news_catalyst_cache (headline_hash, symbol, headline, source, "
        "catalyst_type, sentiment, conviction, is_dilutive, rationale, model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [headline_hash, symbol, f"{symbol} news", "rss", ctype, sentiment,
         conviction, dilutive, "why", "test-model"],
    )


def test_advisory_empty_when_no_rows(store):
    assert query_catalyst_advisory(store) == {}


def test_advisory_returns_per_symbol(store):
    _seed(store, "h1", "BIO", "fda_approval", 0.9, 0.85, False)
    _seed(store, "h2", "SHEL", "offering_dilution", -0.7, 0.8, True)

    adv = query_catalyst_advisory(store)
    assert adv["BIO"]["catalyst_type"] == "fda_approval"
    assert adv["BIO"]["is_dilutive"] is False
    assert adv["BIO"]["sentiment"] == 0.9
    assert adv["SHEL"]["is_dilutive"] is True


def test_advisory_skips_empty_symbol_marker(store):
    # enrichment writes a sentinel row with symbol='' for ticker-less headlines
    _seed(store, "h3", "", "other", 0.0, 0.2, False)
    assert query_catalyst_advisory(store) == {}


def test_feed_empty_when_no_rows(store):
    assert query_catalyst_feed(store) == []


def test_feed_marks_veto_only_above_floor(store):
    # dilutive + conviction >= 0.60 -> the entry would actually be vetoed
    _seed(store, "h1", "AAA", "offering_dilution", -0.5, 0.80, True)
    # dilutive but BELOW the 0.60 floor -> flagged, not vetoed
    _seed(store, "h2", "BBB", "offering_dilution", -0.3, 0.40, True)
    # bullish, not dilutive
    _seed(store, "h3", "CCC", "fda_approval", 0.9, 0.85, False)

    feed = {r["symbol"]: r for r in query_catalyst_feed(store)}
    assert feed["AAA"]["is_dilutive"] is True and feed["AAA"]["vetoed"] is True
    assert feed["BBB"]["is_dilutive"] is True and feed["BBB"]["vetoed"] is False
    assert feed["CCC"]["vetoed"] is False
    assert feed["CCC"]["catalyst_type"] == "fda_approval"


def test_feed_includes_tickerless_reads(store):
    # unlike the advisory, the feed surfaces what the LLM read even with no ticker
    _seed(store, "h4", "", "clinical_trial", 0.8, 0.9, False)
    feed = query_catalyst_feed(store)
    assert len(feed) == 1
    assert feed[0]["symbol"] == ""
    assert feed[0]["catalyst_type"] == "clinical_trial"
