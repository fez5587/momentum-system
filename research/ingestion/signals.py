"""Signal scanning: derive gapper candidates and scanner snapshots.

Reads minute/daily bars from the market database, computes gap %% and
relative volume, and writes ranked rows into scanner_snapshots so the
dashboard and research notebooks can replay what the scanner saw.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

SCANNER_VERSION = "gapper-v2"


@dataclass
class GapperCandidate:
    symbol: str
    price: float
    gap_pct: float
    cumulative_volume: int
    relative_volume: float
    vwap: float | None
    rank: int = 0


def scan_gappers(
    con,
    session_date: date,
    min_gap_pct: float = 5.0,
    min_relative_volume: float = 2.0,
    price_min: float = 1.0,
    price_max: float = 20.0,
    limit: int = 25,
) -> list[GapperCandidate]:
    """Find symbols gapping up on elevated volume for a session."""
    rows = con.execute(
        """
        WITH latest AS (
            SELECT symbol, close, vwap,
                   SUM(volume) OVER (PARTITION BY symbol) AS cum_volume,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol ORDER BY timestamp DESC
                   ) AS rn
            FROM minute_bars
            WHERE session_date = ?
        ),
        prev AS (
            SELECT symbol, close AS prev_close,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol ORDER BY trade_date DESC
                   ) AS rn
            FROM daily_bars
            WHERE trade_date < ?
        ),
        avg_vol AS (
            SELECT symbol, AVG(volume) AS adv
            FROM (
                SELECT symbol, volume,
                       ROW_NUMBER() OVER (
                           PARTITION BY symbol ORDER BY trade_date DESC
                       ) AS rn
                FROM daily_bars
                WHERE trade_date < ?
            )
            WHERE rn <= 20
            GROUP BY symbol
        )
        SELECT l.symbol, l.close, l.vwap, l.cum_volume,
               p.prev_close, a.adv
        FROM latest l
        JOIN prev p ON p.symbol = l.symbol AND p.rn = 1
        LEFT JOIN avg_vol a ON a.symbol = l.symbol
        WHERE l.rn = 1 AND l.close BETWEEN ? AND ?
        """,
        [session_date, session_date, session_date, price_min, price_max],
    ).fetchall()

    candidates: list[GapperCandidate] = []
    for symbol, close, vwap, cum_volume, prev_close, adv in rows:
        if not prev_close or prev_close <= 0:
            continue
        gap_pct = (float(close) - float(prev_close)) / float(prev_close) * 100.0
        rvol = (float(cum_volume) / float(adv)) if adv and adv > 0 else 0.0
        if gap_pct >= min_gap_pct and rvol >= min_relative_volume:
            candidates.append(
                GapperCandidate(
                    symbol=symbol,
                    price=float(close),
                    gap_pct=gap_pct,
                    cumulative_volume=int(cum_volume),
                    relative_volume=rvol,
                    vwap=float(vwap) if vwap is not None else None,
                )
            )
    candidates.sort(key=lambda c: (c.gap_pct * max(c.relative_volume, 0.1)), reverse=True)
    for i, candidate in enumerate(candidates[:limit], start=1):
        candidate.rank = i
    return candidates[:limit]


def store_scanner_snapshot(
    con, candidates: list[GapperCandidate], snapshot_time: datetime | None = None
) -> int:
    """Persist a scan into scanner_snapshots. Returns rows written."""
    ts = (snapshot_time or datetime.now(timezone.utc)).replace(tzinfo=None)
    rows = [
        (
            str(uuid.uuid4()),
            ts,
            c.symbol,
            c.rank,
            c.price,
            c.gap_pct,
            c.cumulative_volume,
            c.relative_volume,
            c.vwap,
            (c.price - c.vwap) / c.vwap * 100.0 if c.vwap else None,
            SCANNER_VERSION,
        )
        for c in candidates
    ]
    if not rows:
        return 0
    con.executemany(
        """
        INSERT INTO scanner_snapshots (
            id, snapshot_time, symbol, rank, price, gap_pct,
            cumulative_volume, relative_volume, vwap, distance_from_vwap,
            scanner_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)
