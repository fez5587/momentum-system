#!/usr/bin/env python3
"""Backfill historical minute + daily bars for a universe of sub-$20 names, so
learn_params.py has enough real sessions to optimize over.

Pulls the current sub-$20 most-actives (liquid names that gap often) plus any
symbols already in the DB, then fetches ~3 weeks of 1-minute bars (one paginated,
batched range request) and 60 days of daily bars for the gap/RVOL baseline.

    PYTHONPATH=. /home/philip/.venvs/momentum/bin/python backfill_history.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from alpaca_paper.client import AlpacaPaperClient
from alpaca_paper.settings import AlpacaPaperSettings
from research.ingestion.discovery import screen_universe
from research.ingestion.market_data import upsert_daily_bars, upsert_minute_bars
from research.multi_schema import open_research_db

load_dotenv()

LOOKBACK_CALENDAR_DAYS = 22  # ~15 trading days


def main():
    client = AlpacaPaperClient(AlpacaPaperSettings.from_env())
    con = open_research_db("market")

    universe = screen_universe(client, 1.0, 20.0, 40)
    existing = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM minute_bars").fetchall()]
    universe = sorted(set(universe) | set(existing))
    print(f"universe ({len(universe)}): {universe}", flush=True)
    if not universe:
        print("no universe (screener empty?) — aborting")
        return

    now = datetime.now(timezone.utc)

    # daily bars (baseline for gap% / RVOL)
    daily = client.get_daily_bars(
        universe, start_iso=(now - timedelta(days=60)).date().isoformat() + "T00:00:00Z"
    )
    drows = sum(upsert_daily_bars(con, s, b) for s, b in daily.items())
    print(f"daily bars: {drows} rows", flush=True)

    # minute bars: one paginated range request (the client batches symbols <=100)
    start = now - timedelta(days=LOOKBACK_CALENDAR_DAYS)
    end = now - timedelta(minutes=20)
    print(f"fetching minute bars {start.date()} .. {end.date()} (this can take a few min)...", flush=True)
    bars = client.get_minute_bars(
        universe,
        start_iso=start.isoformat().replace("+00:00", "Z"),
        end_iso=end.isoformat().replace("+00:00", "Z"),
        feed="iex",
    )
    total = 0
    for sym, sym_bars in bars.items():
        if sym_bars:
            total += upsert_minute_bars(con, sym, sym_bars)
    have = len([s for s in bars if bars[s]])
    print(f"minute bars: {total} rows across {have} symbols", flush=True)

    sessions = con.execute("SELECT COUNT(DISTINCT session_date) FROM minute_bars").fetchone()[0]
    print(f"distinct sessions in DB now: {sessions}", flush=True)


if __name__ == "__main__":
    main()
