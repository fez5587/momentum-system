"""Run intraday backtests against bars stored in the research database.

    python backtest_cli.py --symbol SNDL --date 2026-06-10
    python backtest_cli.py --date 2026-06-10            # all in-band symbols
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from research import query as rq
from research.multi_schema import open_research_db
from strategy.backtest.engine import BacktestEngine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", help="single symbol (default: scan session)")
    parser.add_argument("--date", required=True, help="session date YYYY-MM-DD")
    parser.add_argument("--price-min", type=float, default=1.0)
    parser.add_argument("--price-max", type=float, default=20.0)
    parser.add_argument("--equity", type=float, default=100_000.0)
    args = parser.parse_args(argv)

    session_date = date.fromisoformat(args.date)
    con = open_research_db("market")
    engine = BacktestEngine(equity=args.equity)

    if args.symbol:
        symbols = [args.symbol.upper()]
    else:
        rows = rq.query_session_symbols(
            con, session_date, price_min=args.price_min, price_max=args.price_max
        )
        symbols = [r["symbol"] for r in rows]
        if not symbols:
            print(f"no symbols with minute bars on {session_date} in "
                  f"[{args.price_min}, {args.price_max}] — run fetch_minute_bars.py first")
            return 1

    total_pnl = 0.0
    total_trades = 0
    for symbol in symbols:
        bars = rq.query_minute_bars(con, symbol, session_date)
        if bars.empty:
            print(f"{symbol}: no bars")
            continue
        result = engine.run(
            bars,
            symbol,
            previous_close=rq.query_previous_close(con, symbol, session_date),
            avg_daily_volume=rq.query_avg_daily_volume(con, symbol, session_date),
        )
        s = result.summary()
        total_pnl += s["total_pnl"]
        total_trades += s["trades"]
        print(f"{symbol:>6}  trades={s['trades']:<3} win_rate={s['win_rate']:<6} "
              f"pnl={s['total_pnl']:>9.2f}  signals={s['signals']} evals={s['evaluations']}")

    print(f"\nTOTAL  trades={total_trades}  pnl={total_pnl:.2f}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
