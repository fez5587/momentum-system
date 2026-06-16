"""Discovery screener: top-cap + ETF/band filtering.

Regression for the bug where screen_universe's top*2 headroom made
discover_active_symbols request get_most_actives(top*3=120), exceeding Alpaca's
hard cap of 100 -> HTTP 400 -> swallowed -> empty universe for a whole session.
"""

from research.ingestion.market_data import discover_active_symbols
from research.ingestion.discovery import screen_universe, LEVERAGED_ETFS


class _FakeScreener:
    """Records the `top` it is asked for and prices a few fake symbols."""

    PRICES = {"AAA": 5.0, "BBB": 500.0, "SOXL": 10.0, "CCC": 15.0, "DDD": 2.0}

    def __init__(self):
        self.last_top = None

    def get_most_actives(self, top, by="volume"):
        self.last_top = top
        if top > 100:  # mirror Alpaca: reject top > 100
            raise ValueError("invalid top: should not be larger than 100")
        return [{"symbol": s} for s in self.PRICES]

    def get_latest_trades(self, symbols):
        return {s: {"p": self.PRICES[s]} for s in symbols if s in self.PRICES}


def test_most_actives_top_never_exceeds_100():
    fake = _FakeScreener()
    # top=50 -> top*3=150 historically; must be capped to 100
    out = discover_active_symbols(fake, top=50, price_min=1.0, price_max=20.0)
    assert fake.last_top is not None and fake.last_top <= 100
    assert out, "should return names, not swallow a 400 into []"


def test_discover_active_symbols_filters_price_band():
    fake = _FakeScreener()
    out = discover_active_symbols(fake, top=10, price_min=1.0, price_max=20.0)
    assert "BBB" not in out          # $500 is out of the $1-20 band
    assert {"AAA", "CCC", "DDD"} <= set(out)


def test_screen_universe_filters_leveraged_etfs():
    fake = _FakeScreener()
    out = screen_universe(fake, price_min=1.0, price_max=20.0, top=20)
    assert "SOXL" in LEVERAGED_ETFS   # sanity: it's a known leveraged ETF
    assert "SOXL" not in out          # ...and it's filtered out of the universe
    assert "AAA" in out


def test_screen_universe_empty_when_client_none():
    assert screen_universe(None, 1.0, 20.0, 20) == []
