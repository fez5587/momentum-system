"""Re-ingest bar history from the SIP consolidated tape (free on our keys, 15-min delayed).

WHY (measured 2026-07-02): our entire dataset was IEX-only — ~2-5%% of true consolidated
volume, and IEX even misses whole minutes on thin names (SNDQ 2026-07-01: IEX 188 bars /
1.5M shares vs SIP 391 bars / 60.1M). Every volume-based signal (RVOL, liquidity floors,
volume bursts, dollar-volume strata) and several past research verdicts were computed on
that 2-5%% sample. Daily bars had the same defect (get_daily_bars used the default feed).

  python -m research.sip_backfill              # all history BEFORE today
  python -m research.sip_backfill --date D     # one session (used by the nightly job)

Safe by design: pure upserts through the existing ingestion helpers (ON CONFLICT rewrites
the row), so re-running is idempotent; the live loop's IEX real-time ingest for the CURRENT
session is intentionally untouched (SIP has a 15-min embargo on free keys) — the nightly
job upgrades each finished session to SIP after the close."""

import argparse
import time
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
import os
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from alpaca_paper.client import AlpacaPaperClient          # noqa: E402
from alpaca_paper.settings import AlpacaPaperSettings      # noqa: E402
from research.ingestion.market_data import (               # noqa: E402
    refresh_rolling_volume, upsert_daily_bars, upsert_minute_bars)
from research.multi_schema import open_research_db         # noqa: E402


def refetch_minute_session(con, client, d) -> tuple[int, int]:
    """Re-fetch one session's minute bars (04:00-20:00 ET) from SIP for every symbol
    that already has bars that day. Returns (symbols, rows)."""
    d = d if isinstance(d, date) else date.fromisoformat(str(d))
    syms = [r[0] for r in con.execute(
        "SELECT DISTINCT symbol FROM minute_bars WHERE session_date=?", [d]).fetchall()]
    if not syms:
        return 0, 0
    start = f"{d.isoformat()}T08:00:00Z"                    # 04:00 ET
    end = f"{(d + timedelta(days=1)).isoformat()}T00:00:00Z"  # 20:00 ET
    # cap the end inside the free-key SIP embargo when refetching TODAY
    now_embargo = datetime.utcnow() - timedelta(minutes=16)
    if d == date.today() and now_embargo.isoformat() + "Z" < end:
        end = now_embargo.strftime("%Y-%m-%dT%H:%M:%SZ")
    bars_by_symbol = client.get_minute_bars(syms, start_iso=start, end_iso=end, feed="sip")
    rows = 0
    for sym, bars in bars_by_symbol.items():
        if bars:
            rows += upsert_minute_bars(con, sym, bars, source_provider="alpaca_sip")
    return len(syms), rows


def refetch_daily(con, client, days: int = 60) -> int:
    """Re-fetch daily bars from SIP for every symbol in daily_bars, then rebuild the
    20d rolling volume (which was computed off IEX-thin dailies)."""
    syms = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM daily_bars").fetchall()]
    start = (datetime.utcnow() - timedelta(days=days * 2)).strftime("%Y-%m-%dT00:00:00Z")
    rows = 0
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        try:
            by_symbol = client.get_daily_bars(chunk, start_iso=start, feed="sip")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! daily chunk {i}: {exc}")
            continue
        for sym, bars in by_symbol.items():
            if bars:
                rows += upsert_daily_bars(con, sym, bars)
    refresh_rolling_volume(con)     # full rebuild on true volume
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="SIP re-backfill of bar history")
    ap.add_argument("--date", help="one session date (YYYY-MM-DD); default = all before today")
    ap.add_argument("--skip-daily", action="store_true")
    args = ap.parse_args()
    con = open_research_db("market")
    client = AlpacaPaperClient(AlpacaPaperSettings.from_env())
    t0 = time.time()
    if args.date:
        dates = [date.fromisoformat(args.date)]
    else:
        dates = [r[0] for r in con.execute(
            "SELECT DISTINCT session_date FROM minute_bars WHERE session_date < ? "
            "ORDER BY session_date", [date.today()]).fetchall()]
    total = 0
    for d in dates:
        n, rows = refetch_minute_session(con, client, d)
        total += rows
        print(f"  {d}: {n} symbols -> {rows} SIP rows  ({time.time()-t0:.0f}s)", flush=True)
    if not args.skip_daily:
        drows = refetch_daily(con, client)
        print(f"daily bars re-fetched: {drows} rows + rolling volume rebuilt", flush=True)
    print(f"DONE: {total} minute rows across {len(dates)} sessions in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
