"""Tests for the catalyst-advisory dashboard projection."""

import pytest

from storage.event_store import EventStore
from storage.projections import query_catalyst_advisory


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
