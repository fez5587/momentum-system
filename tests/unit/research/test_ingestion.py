"""Research ingestion tests: session flags, upserts, scans, providers."""

from datetime import datetime, timedelta, timezone

import pytest

from research import query as rq
from research.ingestion.market_data import (
    classify_session,
    ingest_daily_history,
    ingest_live_minute_bars,
    upsert_daily_bars,
    upsert_minute_bars,
)
from research.ingestion.signals import scan_gappers, store_scanner_snapshot
from research.ingestion.watcher_task import ResearchWatchlistProvider
from storage.db import get_connection

SESSION = datetime(2026, 6, 11, tzinfo=timezone.utc)


@pytest.fixture
def con():
    c = get_connection(":memory:")
    yield c
    c.close()


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def minute_payload(start_utc, minutes, base=5.0, step=0.01, volume=50_000):
    bars = []
    price = base
    for i in range(minutes):
        ts = start_utc + timedelta(minutes=i)
        bars.append({"t": iso(ts), "o": price, "h": price + 0.05,
                     "l": price - 0.03, "c": price + step, "v": volume,
                     "vw": price})
        price += step
    return bars


class FakeAlpaca:
    def __init__(self, minute=None, daily=None, fail=False):
        self.minute = minute or {}
        self.daily = daily or {}
        self.fail = fail

    def get_minute_bars(self, symbols, start_iso, end_iso=None, feed=None, limit=10_000):
        if self.fail:
            raise RuntimeError("api down")
        return {s: self.minute.get(s, []) for s in symbols}

    def get_daily_bars(self, symbols, start_iso, end_iso=None):
        if self.fail:
            raise RuntimeError("api down")
        return {s: self.daily.get(s, []) for s in symbols}


def test_classify_session_boundaries():
    # 9:30 ET == 13:30 UTC in June (EDT)
    def at(h, m):
        return classify_session(datetime(2026, 6, 11, h, m, tzinfo=timezone.utc))

    assert at(13, 29)[1:] == (True, False, False)   # 9:29 ET premarket
    assert at(13, 30)[1:] == (False, True, False)   # 9:30 ET regular open
    assert at(19, 59)[1:] == (False, True, False)   # 3:59 ET regular
    assert at(20, 0)[1:] == (False, False, True)    # 4:00 ET afterhours
    assert at(7, 0)[1:] == (False, False, False)    # 3:00 ET closed


def test_upsert_minute_bars_sets_session_flags(con):
    # 13:00 UTC = 9:00 ET premarket; 14:00 UTC = 10:00 ET regular
    bars = minute_payload(SESSION.replace(hour=13), 5) + \
           minute_payload(SESSION.replace(hour=14), 5, base=5.2)
    count = upsert_minute_bars(con, "ABCD", bars)
    assert count == 10
    pre, reg = con.execute(
        "SELECT SUM(CASE WHEN is_premarket THEN 1 ELSE 0 END),"
        "       SUM(CASE WHEN is_regular_hours THEN 1 ELSE 0 END)"
        " FROM minute_bars"
    ).fetchone()
    assert (pre, reg) == (5, 5)
    # idempotent: re-ingesting the same bars must not duplicate
    upsert_minute_bars(con, "ABCD", bars)
    total = con.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0]
    assert total == 10


def test_upsert_daily_bars_derives_previous_close(con):
    daily = [
        {"t": iso(SESSION - timedelta(days=2)), "o": 4, "h": 4.5, "l": 3.9, "c": 4.2, "v": 1_000_000},
        {"t": iso(SESSION - timedelta(days=1)), "o": 4.2, "h": 4.4, "l": 4.0, "c": 4.1, "v": 900_000},
    ]
    assert upsert_daily_bars(con, "ABCD", daily) == 2
    prev = rq.query_previous_close(con, "ABCD", SESSION.date())
    assert prev == pytest.approx(4.1)
    adv = rq.query_avg_daily_volume(con, "ABCD", SESSION.date())
    assert adv == pytest.approx(950_000)


def test_ingest_live_minute_bars_with_fake_client(con):
    client = FakeAlpaca(minute={"ABCD": minute_payload(SESSION.replace(hour=14), 30)})
    result = ingest_live_minute_bars(con, client, ["ABCD"], lookback_minutes=60)
    assert result.minute_rows == 30
    assert result.errors == []
    bars = rq.query_minute_bars(con, "ABCD", SESSION.date())
    assert len(bars) == 30
    assert list(bars["timestamp"]) == sorted(bars["timestamp"])


def test_ingest_failures_are_reported_not_raised(con):
    result = ingest_live_minute_bars(con, FakeAlpaca(fail=True), ["ABCD"])
    assert result.minute_rows == 0
    assert result.errors
    result = ingest_daily_history(con, FakeAlpaca(fail=True), ["ABCD"])
    assert result.daily_rows == 0
    assert result.errors


def test_session_symbols_price_band_configurable(con):
    upsert_minute_bars(con, "CHEAP", minute_payload(SESSION.replace(hour=14), 12, base=5.0))
    upsert_minute_bars(con, "MEGA", minute_payload(SESSION.replace(hour=14), 12, base=500.0))
    in_band = rq.query_session_symbols(con, SESSION.date(), price_min=1, price_max=20)
    assert [r["symbol"] for r in in_band] == ["CHEAP"]
    # the old hardcoded 1-20 band was the bug; a wide band must include MEGA
    wide = rq.query_session_symbols(con, SESSION.date(), price_min=0, price_max=10_000)
    assert {r["symbol"] for r in wide} == {"CHEAP", "MEGA"}


def test_gapper_scan_and_snapshot(con):
    # previous close 4.10, today ~5.0+ -> gap > 20%; adv 950k vs cum volume high
    upsert_daily_bars(con, "GAPR", [
        {"t": iso(SESSION - timedelta(days=2)), "o": 4, "h": 4.5, "l": 3.9, "c": 4.2, "v": 1_000_000},
        {"t": iso(SESSION - timedelta(days=1)), "o": 4.2, "h": 4.4, "l": 4.0, "c": 4.1, "v": 900_000},
    ])
    upsert_minute_bars(con, "GAPR", minute_payload(SESSION.replace(hour=14), 60, base=5.0, volume=80_000))
    candidates = scan_gappers(con, SESSION.date(), min_gap_pct=5, min_relative_volume=2)
    assert candidates and candidates[0].symbol == "GAPR"
    assert candidates[0].gap_pct > 20
    assert store_scanner_snapshot(con, candidates) == len(candidates)
    rows = con.execute("SELECT symbol, rank FROM scanner_snapshots").fetchall()
    assert rows[0] == ("GAPR", 1)


def test_research_watchlist_provider_feeds_watcher(con):
    upsert_daily_bars(con, "GAPR", [
        {"t": iso(SESSION - timedelta(days=1)), "o": 4.2, "h": 4.4, "l": 4.0, "c": 4.1, "v": 900_000},
    ])
    upsert_minute_bars(con, "GAPR", minute_payload(SESSION.replace(hour=14), 15, base=5.0))
    provider = ResearchWatchlistProvider(con, price_min=1, price_max=20)
    candidates = provider.get_candidates(SESSION.date())
    assert candidates[0].symbol == "GAPR"
    assert candidates[0].previous_close == pytest.approx(4.1)
    bars = provider.get_bars("GAPR", SESSION.date())
    assert len(bars) == 15
