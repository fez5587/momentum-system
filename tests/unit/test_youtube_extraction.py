"""Pure-helper tests for the claim extractor — the anti-hallucination quote match + chunking.
The Ollama call itself is integration-tested live (not mocked here)."""

from youtube_claims.extraction import _chunks, _norm, _normalize, _quote_supported


def test_norm_collapses_whitespace():
    assert _norm("  A\nB   c ") == "a b c"


def test_quote_supported_matches_across_segment_joins():
    # the caption text is fragmented (joined with spaces); a quote spanning the join still matches
    ntext = _norm("as it broke through 12\ni took 3000 off the table at 12 and 13 cents")
    assert _quote_supported("as it broke through 12 I took 3000 off the table", ntext)


def test_quote_supported_rejects_hallucination():
    ntext = _norm("the trader discussed a reverse split squeeze")
    assert not _quote_supported("AAPL will hit 300 next week", ntext)
    assert not _quote_supported("", ntext)


def test_normalize_drops_claim_without_verbatim_quote():
    assert _normalize({"asset_ticker": "aapl", "verbatim_quote": ""}, 0, 6) is None
    ok = _normalize({"asset_ticker": "aapl", "verbatim_quote": "aapl broke out"}, 0, 6)
    assert ok and ok["asset_ticker"] == "AAPL" and ok["timestamp_start"] == 0


def test_chunks_group_segments_with_time_span():
    segs = [{"start": float(i), "end": float(i + 1), "text": "x" * 100} for i in range(200)]
    wins = list(_chunks(segs, max_chars=1000))
    assert len(wins) > 1
    buf, start, end = wins[0]
    assert start == 0.0 and end is not None and len(buf) >= 1
