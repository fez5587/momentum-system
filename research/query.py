"""Read-side queries over the research market database."""

from __future__ import annotations

from datetime import date

import pandas as pd


def query_session_symbols(
    con,
    session_date: date,
    price_min: float = 1.0,
    price_max: float = 20.0,
    limit: int = 50,
) -> list[dict]:
    """Symbols with minute bars today whose latest close is inside the band.

    Pass price_min=0 / price_max=inf to disable the band.
    """
    rows = con.execute(
        """
        WITH latest AS (
            SELECT symbol, close, volume,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol ORDER BY timestamp DESC
                   ) AS rn,
                   COUNT(*) OVER (PARTITION BY symbol) AS bar_count
            FROM minute_bars
            WHERE session_date = ?
        )
        SELECT symbol, close AS last_price, bar_count
        FROM latest
        WHERE rn = 1 AND close BETWEEN ? AND ?
        ORDER BY bar_count DESC
        LIMIT ?
        """,
        [session_date, price_min, price_max, limit],
    ).fetchall()
    return [
        {"symbol": r[0], "last_price": float(r[1]), "bar_count": int(r[2])}
        for r in rows
    ]


def query_minute_bars(con, symbol: str, session_date: date) -> pd.DataFrame:
    """All of today's minute bars for a symbol, chronological."""
    return con.execute(
        """
        SELECT symbol, timestamp, session_date, is_premarket, is_regular_hours,
               open, high, low, close, volume, vwap, quality_score
        FROM minute_bars
        WHERE symbol = ? AND session_date = ?
        ORDER BY timestamp ASC
        """,
        [symbol, session_date],
    ).df()


def query_previous_close(con, symbol: str, session_date: date) -> float | None:
    row = con.execute(
        """
        SELECT close FROM daily_bars
        WHERE symbol = ? AND trade_date < ?
        ORDER BY trade_date DESC LIMIT 1
        """,
        [symbol, session_date],
    ).fetchone()
    return float(row[0]) if row else None


def query_avg_daily_volume(con, symbol: str, session_date: date, days: int = 20) -> float | None:
    row = con.execute(
        """
        SELECT AVG(volume) FROM (
            SELECT volume FROM daily_bars
            WHERE symbol = ? AND trade_date < ?
            ORDER BY trade_date DESC LIMIT ?
        )
        """,
        [symbol, session_date, days],
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None
