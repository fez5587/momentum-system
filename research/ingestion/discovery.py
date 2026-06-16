"""Symbol discovery: screen sub-$20 most-actives, rank gappers, emit events.

The strategy targets $1-20 small-cap momentum names, NOT mega-caps. This module
is the default watchlist source (replacing a hardcoded mega-cap list):

  1. screen Alpaca most-actives down to the configured price band,
  2. backfill daily history so gap %% / RVOL have a baseline,
  3. run the gapper scan (gap %% + relative volume) over ingested bars,
  4. persist a scanner snapshot AND emit a structured discovery event so the
     event store / dashboard can SEE what the scanner found and why.

Emitting events is the WS1 fix: discovery and bar collection are no longer
silent — they land as queryable rows in Postgres.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from research.ingestion.market_data import (
    discover_active_symbols,
    ingest_daily_history,
)
from research.ingestion.rss import ingest_all_feeds
from research.ingestion.signals import scan_gappers, store_scanner_snapshot
from storage.event_schema import EventMode, ModuleTickEvent


@dataclass
class DiscoveryResult:
    universe: list[str] = field(default_factory=list)   # screened $1-20 most-actives
    gappers: list = field(default_factory=list)         # ranked GapperCandidate list
    news: list[str] = field(default_factory=list)       # catalyst-driven tickers from news
    daily_rows: int = 0
    errors: list[str] = field(default_factory=list)


# Common leveraged / inverse ETFs & ETNs. The strategy targets small-cap stocks,
# not these; they pollute a sub-$20 most-actives screen. Extend via DISCOVER_EXCLUDE.
LEVERAGED_ETFS = {
    "SOXL", "SOXS", "TQQQ", "SQQQ", "TNA", "TZA", "SPXL", "SPXS", "SPXU", "UPRO",
    "UVXY", "SVXY", "VIXY", "TSLL", "TSLQ", "TSLS", "TSLZ", "NVDL", "NVD", "NVDU",
    "NVDD", "BITO", "BITX", "BITI", "ETHU", "ETHD", "FNGU", "FNGD", "LABU", "LABD",
    "YINN", "YANG", "FAS", "FAZ", "UDOW", "SDOW", "TMF", "TMV", "BOIL", "KOLD",
    "UCO", "SCO", "JNUG", "JDST", "NUGT", "DUST", "GUSH", "DRIP", "MSTU", "MSTX",
    "MSTZ", "CONL", "AMDL", "GGLL", "AAPU", "AAPD", "AMZU", "METU", "QQQU",
}


def _excluded_symbols() -> set[str]:
    extra = {
        s.strip().upper()
        for s in os.environ.get("DISCOVER_EXCLUDE", "").split(",")
        if s.strip()
    }
    return LEVERAGED_ETFS | extra


def screen_universe(
    client,
    price_min: float = 1.0,
    price_max: float = 20.0,
    top: int = 20,
    exclude_etfs: bool = True,
    retries: int = 3,
) -> list[str]:
    """Sub-$20 most-actives universe with leveraged ETFs filtered out.

    Retries on a transient empty result (the screener can briefly rate-limit),
    so a hiccup doesn't collapse the watchlist. Returns an empty list only if
    the screener stays unavailable; the caller then uses its own fallback.
    Requests extra headroom so we still land ~`top` stocks after the ETF filter.
    """
    if client is None:
        return []
    names: list[str] = []
    for attempt in range(max(1, retries)):
        try:
            names = discover_active_symbols(
                client, top=top * 2, price_min=price_min, price_max=price_max
            )
        except Exception:  # noqa: BLE001 - screener can need a paid feed
            names = []
        if names:
            break
        if attempt < retries - 1:
            time.sleep(1.5)
    if exclude_etfs and names:
        names = [s for s in names if s not in _excluded_symbols()]
    return names[:top]


def _news_feeds() -> dict[str, str]:
    """Parse NEWS_FEEDS env: 'name1=url1,name2=url2'. Empty -> news disabled."""
    feeds: dict[str, str] = {}
    for part in os.environ.get("NEWS_FEEDS", "").split(","):
        part = part.strip()
        if "=" in part:
            name, url = part.split("=", 1)
            if name.strip() and url.strip():
                feeds[name.strip()] = url.strip()
    return feeds


def news_candidates(
    con, feeds: dict[str, str] | None = None, lookback_hours: int = 12, limit: int = 15
) -> list[str]:
    """Catalyst-driven candidates: tickers mentioned in recently ingested news.

    No-op (returns []) unless news feeds are configured (NEWS_FEEDS env or the
    ``feeds`` arg). Never raises — news is an enhancement, not a dependency.
    """
    feeds = feeds if feeds is not None else _news_feeds()
    if not feeds:
        return []
    try:
        ingest_all_feeds(con, feeds)
    except Exception:  # noqa: BLE001
        pass
    cutoff = datetime.utcnow() - timedelta(hours=lookback_hours)
    try:
        rows = con.execute(
            "SELECT raw_tickers FROM raw_news_items "
            "WHERE raw_tickers <> '' AND fetched_at >= ? "
            "ORDER BY fetched_at DESC LIMIT 500",
            [cutoff],
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    seen: set = set()
    out: list[str] = []
    for (raw_tickers,) in rows:
        for ticker in (raw_tickers or "").split(","):
            ticker = ticker.strip().upper()
            if ticker and ticker not in seen:
                seen.add(ticker)
                out.append(ticker)
    return out[:limit]


def run_discovery(
    store,
    research_con,
    client,
    session_date: date,
    *,
    mode: EventMode = EventMode.PAPER,
    correlation_id: str | None = None,
    price_min: float = 1.0,
    price_max: float = 20.0,
    top: int = 20,
    min_gap_pct: float = 5.0,
    min_relative_volume: float = 2.0,
    backfill_daily: bool = True,
) -> DiscoveryResult:
    """Screen + scan + persist + emit one discovery pass."""
    t0 = time.monotonic()
    result = DiscoveryResult()

    result.universe = screen_universe(
        client, price_min=price_min, price_max=price_max, top=top
    )
    # catalyst-driven candidates from recent news (active only if NEWS_FEEDS set)
    result.news = news_candidates(research_con)
    for ticker in result.news:
        if ticker not in result.universe:
            result.universe.append(ticker)

    # Backfill daily history for the universe so gap%/RVOL have a baseline.
    if backfill_daily and result.universe and client is not None:
        try:
            daily = ingest_daily_history(research_con, client, result.universe, days=30)
            result.daily_rows = daily.daily_rows
            result.errors.extend(daily.errors)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"daily backfill failed: {exc}")

    # Rank gappers over whatever minute bars are ingested for the session.
    try:
        result.gappers = scan_gappers(
            research_con,
            session_date,
            min_gap_pct=min_gap_pct,
            min_relative_volume=min_relative_volume,
            price_min=price_min,
            price_max=price_max,
        )
        store_scanner_snapshot(research_con, result.gappers)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"gapper scan failed: {exc}")

    duration_ms = (time.monotonic() - t0) * 1000.0
    store.emit(
        ModuleTickEvent(
            timestamp=datetime.now(),
            mode=mode,
            correlation_id=correlation_id,
            message=(
                f"discovery: {len(result.universe)} names in "
                f"${price_min:g}-${price_max:g}, {len(result.gappers)} gappers"
            ),
            module="discovery",
            stage="completed",
            duration_ms=duration_ms,
            input_count=len(result.universe),
            output_count=len(result.gappers),
            metrics={
                "price_band": [price_min, price_max],
                "universe": result.universe,
                "news": result.news,
                "gappers": [
                    {
                        "symbol": g.symbol,
                        "price": round(g.price, 2),
                        "gap_pct": round(g.gap_pct, 1),
                        "rvol": round(g.relative_volume, 1),
                        "rank": g.rank,
                    }
                    for g in result.gappers
                ],
            },
            errors=[{"error": e} for e in result.errors],
        )
    )
    return result
