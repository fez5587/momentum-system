"""End-to-end (Ollama mocked): raw headline -> enrich -> dashboard advisory.

Exercises the full Phase 1 path on a single Postgres connection: seed
raw_news_items, run the enrichment pass with the LLM call patched, then read it
back through the dashboard projection exactly as api/main.py snapshots() does.
"""

from types import SimpleNamespace

import pytest

from research.ingestion import news_enrichment as ne
from storage.db import get_connection
from storage.projections import query_catalyst_advisory


class _Cfg:
    host = "http://localhost:11434"
    model = "test-model"
    timeout_seconds = 5
    temperature = 0.3
    max_tokens = 128
    enrichment_lookback_hours = 12
    enrichment_batch_limit = 50


@pytest.fixture
def con():
    c = get_connection(":memory:")
    yield c
    c.close()


def test_headline_flows_to_dashboard_advisory(con, monkeypatch):
    # 1. a fresh dilutive-offering headline lands in the raw table (as RSS would)
    con.execute(
        "INSERT INTO raw_news_items (id, fetch_attempt_id, source, raw_url, raw_title, "
        "raw_published_at, raw_body_snippet, raw_tickers, payload_hash, parser_version, "
        "fetched_at, ingest_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "current_timestamp, ?)",
        ["h1", "fa1", "prnewswire", "http://x/1",
         "SHEL Inc. announces $30M registered direct offering",
         "", "pricing of offering", "SHEL", "h1", "rss-v2", "run1"],
    )

    # 2. Ollama replies with a structured classification (patched — no network).
    #    urlopen is used as a context manager in classify_headline.
    import json

    class _CtxResp:
        def __init__(self, body):
            self._b = body
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(
        ne.urllib.request, "urlopen",
        lambda *a, **k: _CtxResp(json.dumps({"response": json.dumps({
            "catalyst_type": "offering_dilution", "sentiment": -0.8,
            "conviction": 0.9, "is_dilutive": True,
            "rationale": "registered direct offering dilutes holders",
        })}).encode()),
    )

    # 3. the scheduled enrichment pass runs
    res = ne.enrich_recent_news(con, _Cfg())
    assert res["enriched"] == 1

    # 4. the dashboard reads it via the projection (store.con == this connection)
    store = SimpleNamespace(con=con)
    advisory = query_catalyst_advisory(store)
    assert "SHEL" in advisory
    assert advisory["SHEL"]["is_dilutive"] is True
    assert advisory["SHEL"]["catalyst_type"] == "offering_dilution"

    # 5. the previously-dead news_events.sentiment column is now populated
    sent = con.execute(
        "SELECT sentiment FROM news_events WHERE symbol = ?", ["SHEL"]
    ).fetchone()
    assert sent is not None and sent[0] == -0.8
