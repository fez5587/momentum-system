"""Fetch 1-minute Alpaca bars into the research market database.

    python fetch_minute_bars.py AAPL TSLA --lookback 390
    python fetch_minute_bars.py SNDL --daily-days 30
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from alpaca_paper.client import AlpacaPaperClient
from alpaca_paper.settings import AlpacaPaperSettings
from research.ingestion.market_data import ingest_daily_history, ingest_live_minute_bars
from research.multi_schema import open_research_db


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+", help="ticker symbols")
    parser.add_argument("--lookback", type=int, default=390, help="minutes of 1-min bars")
    parser.add_argument("--daily-days", type=int, default=30, help="days of daily backfill")
    parser.add_argument("--feed", default=None, help="alpaca feed (default: settings/iex)")
    args = parser.parse_args(argv)

    settings = AlpacaPaperSettings.from_env()
    if not settings.is_configured:
        print("error: set ALPACA_API_KEY and ALPACA_SECRET_KEY (see .env.example)")
        return 1

    client = AlpacaPaperClient(settings)
    con = open_research_db("market")
    symbols = [s.upper() for s in args.symbols]

    daily = ingest_daily_history(con, client, symbols, days=args.daily_days)
    print(f"daily bars: {daily.daily_rows} rows" + (f" errors={daily.errors}" if daily.errors else ""))

    minute = ingest_live_minute_bars(
        con, client, symbols, lookback_minutes=args.lookback, feed=args.feed
    )
    print(f"minute bars: {minute.minute_rows} rows for {sorted(set(minute.symbols))}"
          + (f" errors={minute.errors}" if minute.errors else ""))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
