"""Research utilities CLI.

    python research_cli.py symbols --date 2026-06-11
    python research_cli.py gappers --date 2026-06-11
    python research_cli.py news
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

from dotenv import load_dotenv

from research import query as rq
from research.ingestion.rss import ingest_all_feeds
from research.ingestion.signals import scan_gappers, store_scanner_snapshot
from research.multi_schema import open_research_db

DEFAULT_FEEDS = {
    "globenewswire_public": "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
    "prnewswire_news": "https://www.prnewswire.com/rss/news-releases-list.rss",
}


def cmd_symbols(args) -> int:
    con = open_research_db("market")
    rows = rq.query_session_symbols(
        con, args.session_date, price_min=args.price_min,
        price_max=args.price_max, limit=args.limit,
    )
    if not rows:
        print("no symbols — ingest minute bars first (fetch_minute_bars.py)")
        return 1
    for r in rows:
        print(f"{r['symbol']:>6}  last={r['last_price']:<8.2f} bars={r['bar_count']}")
    return 0


def cmd_gappers(args) -> int:
    con = open_research_db("market")
    candidates = scan_gappers(
        con, args.session_date,
        min_gap_pct=args.min_gap, min_relative_volume=args.min_rvol,
        price_min=args.price_min, price_max=args.price_max,
    )
    if not candidates:
        print("no gappers found for this session")
        return 0
    stored = store_scanner_snapshot(con, candidates)
    for c in candidates:
        print(f"#{c.rank:<2} {c.symbol:>6}  gap={c.gap_pct:>6.1f}%  rvol={c.relative_volume:>5.1f}x "
              f"price={c.price:<8.2f} vol={c.cumulative_volume:,}")
    print(f"\nstored {stored} rows in scanner_snapshots")
    return 0


def cmd_news(args) -> int:
    con = open_research_db("news")
    feeds = dict(DEFAULT_FEEDS)
    extra = os.environ.get("NEWS_RSS_FEEDS", "")
    for pair in extra.split(";"):
        if "=" in pair:
            name, url = pair.split("=", 1)
            feeds[name.strip()] = url.strip()
    for result in ingest_all_feeds(con, feeds):
        status = result.error or f"http {result.http_status}"
        print(f"{result.source:<28} items={result.item_count:<4} new={len(result.new_items):<4} ({status})")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--date", default=date.today().isoformat())
        p.add_argument("--price-min", type=float, default=1.0)
        p.add_argument("--price-max", type=float, default=20.0)

    p_sym = sub.add_parser("symbols", help="list symbols with bars for a session")
    add_common(p_sym)
    p_sym.add_argument("--limit", type=int, default=50)

    p_gap = sub.add_parser("gappers", help="scan for gappers and store snapshot")
    add_common(p_gap)
    p_gap.add_argument("--min-gap", type=float, default=5.0)
    p_gap.add_argument("--min-rvol", type=float, default=2.0)

    sub.add_parser("news", help="ingest RSS feeds into news.duckdb")

    args = parser.parse_args(argv)
    if hasattr(args, "date"):
        args.session_date = date.fromisoformat(args.date)
    return {"symbols": cmd_symbols, "gappers": cmd_gappers, "news": cmd_news}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
