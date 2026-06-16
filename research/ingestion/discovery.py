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

import time
from dataclasses import dataclass, field
from datetime import date, datetime

from research.ingestion.market_data import (
    discover_active_symbols,
    ingest_daily_history,
)
from research.ingestion.signals import scan_gappers, store_scanner_snapshot
from storage.event_schema import EventMode, ModuleTickEvent


@dataclass
class DiscoveryResult:
    universe: list[str] = field(default_factory=list)   # screened $1-20 most-actives
    gappers: list = field(default_factory=list)         # ranked GapperCandidate list
    daily_rows: int = 0
    errors: list[str] = field(default_factory=list)


def screen_universe(
    client,
    price_min: float = 1.0,
    price_max: float = 20.0,
    top: int = 20,
) -> list[str]:
    """The sub-$20 most-actives universe (empty list if the screener is down)."""
    if client is None:
        return []
    try:
        return discover_active_symbols(
            client, top=top, price_min=price_min, price_max=price_max
        )
    except Exception:  # noqa: BLE001 - screener can need a paid feed
        return []


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
