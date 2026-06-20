"""Alpaca News API ingestion -> raw_news_items (same landing table as RSS)."""

import pytest

from research.ingestion.alpaca_news import ingest_alpaca_news, normalize_item
from storage.db import get_connection


@pytest.fixture
def con():
    c = get_connection(":memory:")
    yield c
    c.close()


class _FakeClient:
    def __init__(self, news=None, raises=False):
        self._news = news or []
        self.raises = raises
        self.calls = []

    def get_news(self, symbols=None, limit=50):
        self.calls.append({"symbols": symbols, "limit": limit})
        if self.raises:
            raise RuntimeError("news api down")
        return self._news


def _item(nid, headline, symbols, **kw):
    d = {"id": nid, "headline": headline, "symbols": symbols,
         "url": f"http://x/{nid}", "summary": "snippet",
         "created_at": "2026-06-20T14:00:00Z", "source": "benzinga"}
    d.update(kw)
    return d


def test_normalize_uses_alpaca_symbols_as_tickers():
    n = normalize_item(_item("1", "  ICCM jumps on data  ", ["ICCM", "CDT", "ICCM"]))
    assert n["news_id"] == "1"
    assert n["title"] == "ICCM jumps on data"      # stripped
    assert n["tickers"] == "CDT,ICCM"              # sorted + deduped, no regex
    assert n["src"] == "benzinga"
    assert n["published"] == "2026-06-20T14:00:00Z"


def test_ingest_lands_rows_with_alpaca_source(con):
    client = _FakeClient([_item("100", "WPRT moves", ["WPRT"]),
                          _item("101", "CDT boost", ["CDT", "ICCM"])])
    r = ingest_alpaca_news(con, client, symbols=["WPRT", "CDT"])
    assert r.item_count == 2 and len(r.new_items) == 2 and r.error is None
    rows = con.execute(
        "SELECT source, raw_tickers, raw_title FROM raw_news_items "
        "WHERE source LIKE 'alpaca%' ORDER BY raw_title").fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "alpaca:benzinga"
    assert any("WPRT" in (t or "") for _, t, _ in rows)


def test_ingest_dedupes_on_second_pass(con):
    client = _FakeClient([_item("200", "same story", ["AAA"])])
    assert len(ingest_alpaca_news(con, client).new_items) == 1
    # same Alpaca id -> nothing new the second time
    r2 = ingest_alpaca_news(con, client)
    assert len(r2.new_items) == 0 and r2.item_count == 1


def test_ingest_never_raises_when_client_fails(con):
    client = _FakeClient(raises=True)
    r = ingest_alpaca_news(con, client)            # must not raise
    assert r.error is not None and r.new_items == []
    # the failed attempt is still recorded (with the error) for observability
    att = con.execute(
        "SELECT error_msg FROM raw_fetch_attempts WHERE source='alpaca'").fetchall()
    assert att and att[0][0]


def test_ingest_forwards_symbol_filter(con):
    client = _FakeClient([])
    ingest_alpaca_news(con, client, symbols=["AAA", "BBB"], limit=10)
    assert client.calls[0]["symbols"] == ["AAA", "BBB"]
    assert client.calls[0]["limit"] == 10
