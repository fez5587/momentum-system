"""Watchlist providers and watcher task glue.

Two providers implement the WatchlistProvider protocol:

* ResearchWatchlistProvider — reads candidates and bars from the research
  market.duckdb (populated by research.ingestion.market_data). The price
  band is configurable via WATCHER_PRICE_MIN / WATCHER_PRICE_MAX, fixing
  the old hardcoded ``close BETWEEN 1 AND 20`` filter that silently
  excluded every ingested symbol.
* LiveWatchlistProvider — takes an explicit symbol list and pulls bars
  straight from Alpaca, for running without a research database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from research import query as research_query
from research.ingestion.market_data import classify_session, parse_alpaca_timestamp
from runtime.watcher import WatchCandidate, Watcher, WatcherTickResult


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass
class ResearchWatchlistProvider:
    """Candidates + bars from the research market database."""

    con: object
    price_min: float = field(default_factory=lambda: _env_float("WATCHER_PRICE_MIN", 1.0))
    price_max: float = field(default_factory=lambda: _env_float("WATCHER_PRICE_MAX", 20.0))
    limit: int = 50
    # symbols manually injected from the dashboard; always evaluated regardless
    # of the price-band query (e.g. a ticker not yet ingested)
    extra_symbols: set = field(default_factory=set)

    def add_symbol(self, symbol: str) -> None:
        self.extra_symbols.add(symbol.upper())

    def get_candidates(self, session_date: date) -> list[WatchCandidate]:
        rows = research_query.query_session_symbols(
            self.con,
            session_date,
            price_min=self.price_min,
            price_max=self.price_max,
            limit=self.limit,
        )
        seen = set()
        candidates = []
        for row in rows:
            symbol = row["symbol"]
            seen.add(symbol)
            candidates.append(
                WatchCandidate(
                    symbol=symbol,
                    last_price=row.get("last_price"),
                    previous_close=research_query.query_previous_close(
                        self.con, symbol, session_date
                    ),
                    avg_daily_volume=research_query.query_avg_daily_volume(
                        self.con, symbol, session_date
                    ),
                    source="research",
                )
            )
        # always include manually injected symbols
        for symbol in sorted(self.extra_symbols - seen):
            candidates.append(
                WatchCandidate(
                    symbol=symbol,
                    previous_close=research_query.query_previous_close(
                        self.con, symbol, session_date
                    ),
                    avg_daily_volume=research_query.query_avg_daily_volume(
                        self.con, symbol, session_date
                    ),
                    source="manual",
                )
            )
        return candidates

    def get_bars(self, symbol: str, session_date: date) -> pd.DataFrame:
        return research_query.query_minute_bars(self.con, symbol, session_date)


@dataclass
class LiveWatchlistProvider:
    """Explicit symbols, bars fetched live from Alpaca (no database needed)."""

    client: object
    symbols: list[str]
    lookback_minutes: int = 420
    _daily_cache: dict[str, list[dict]] = field(default_factory=dict)

    def _daily_bars(self, symbol: str) -> list[dict]:
        if symbol not in self._daily_cache:
            start = (datetime.now(timezone.utc) - timedelta(days=60)).date()
            try:
                payload = self.client.get_daily_bars(
                    [symbol], start_iso=f"{start.isoformat()}T00:00:00Z"
                )
                self._daily_cache[symbol] = payload.get(symbol, [])
            except Exception:  # noqa: BLE001
                self._daily_cache[symbol] = []
        return self._daily_cache[symbol]

    def get_candidates(self, session_date: date) -> list[WatchCandidate]:
        candidates = []
        for symbol in self.symbols:
            daily = self._daily_bars(symbol)
            prev_close = None
            adv = None
            if daily:
                # last bar strictly before the session date
                prior = [
                    b for b in daily
                    if parse_alpaca_timestamp(b["t"]).date() < session_date
                ]
                if prior:
                    prev_close = float(prior[-1]["c"])
                    recent = prior[-20:]
                    adv = sum(float(b["v"]) for b in recent) / len(recent)
            candidates.append(
                WatchCandidate(
                    symbol=symbol,
                    previous_close=prev_close,
                    avg_daily_volume=adv,
                    source="live",
                )
            )
        return candidates

    def get_bars(self, symbol: str, session_date: date) -> pd.DataFrame:
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=self.lookback_minutes)
        try:
            payload = self.client.get_minute_bars(
                [symbol],
                start_iso=start.isoformat().replace("+00:00", "Z"),
                end_iso=(now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            )
            bars = payload.get(symbol, [])
        except Exception:  # noqa: BLE001
            bars = []
        rows = []
        for bar in bars:
            ts = parse_alpaca_timestamp(bar["t"])
            bar_date, is_pre, is_reg, _ = classify_session(ts)
            if bar_date != session_date:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "timestamp": ts.astimezone(timezone.utc).replace(tzinfo=None),
                    "session_date": bar_date,
                    "is_premarket": is_pre,
                    "is_regular_hours": is_reg,
                    "open": float(bar["o"]),
                    "high": float(bar["h"]),
                    "low": float(bar["l"]),
                    "close": float(bar["c"]),
                    "volume": int(bar["v"]),
                    "vwap": float(bar.get("vw") or bar["c"]),
                    "quality_score": 1.0,
                }
            )
        return pd.DataFrame(rows)


def run_watcher_tick(
    watcher: Watcher, session_date: date | None = None
) -> WatcherTickResult:
    """One watcher tick with a safe default session date (US/Eastern today)."""
    if session_date is None:
        session_date, _, _, _ = classify_session(datetime.now(timezone.utc))
    return watcher.tick(session_date)
