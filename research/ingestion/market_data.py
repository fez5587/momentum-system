"""Market data ingestion: Alpaca bars -> research market.duckdb.

This module fixes the root cause of "live paper trading never worked":
the watcher reads *minute* bars from the research database, but the old
ingestion only ever wrote *daily* bars for a handful of mega-caps and
hardcoded ``is_regular_hours = TRUE`` on every row. Here we:

* ingest 1-minute bars (IEX feed by default) for a configurable symbol list,
* derive ``is_premarket`` / ``is_regular_hours`` / ``is_afterhours`` from the
  bar timestamp in US/Eastern (regular session 09:30-16:00),
* backfill daily bars so the watcher can compute gap %% and relative volume.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
PREMARKET_OPEN = time(4, 0)
AFTERHOURS_CLOSE = time(20, 0)

logger = logging.getLogger(__name__)


def classify_session(ts_utc: datetime) -> tuple[date, bool, bool, bool]:
    """Return (session_date, is_premarket, is_regular_hours, is_afterhours)."""
    if ts_utc.tzinfo is None:
        ts_utc = ts_utc.replace(tzinfo=timezone.utc)
    local = ts_utc.astimezone(EASTERN)
    t = local.time()
    is_pre = PREMARKET_OPEN <= t < REGULAR_OPEN
    is_reg = REGULAR_OPEN <= t < REGULAR_CLOSE
    is_aft = REGULAR_CLOSE <= t < AFTERHOURS_CLOSE
    return local.date(), is_pre, is_reg, is_aft


def parse_alpaca_timestamp(raw: str) -> datetime:
    """Alpaca returns RFC3339 timestamps like '2026-06-11T13:30:00Z'."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


@dataclass
class IngestionResult:
    minute_rows: int = 0
    daily_rows: int = 0
    symbols: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # per-symbol rows written this pass (incl. 0 for requested-but-empty symbols),
    # so "are bars collected?" is answerable per symbol, not just in aggregate.
    per_symbol: dict = field(default_factory=dict)


def upsert_minute_bars(
    con,
    symbol: str,
    bars: list[dict],
    source_provider: str = "alpaca_iex",
) -> int:
    """Insert/replace Alpaca minute bars for one symbol. Returns row count."""
    rows = []
    for bar in bars:
        ts = parse_alpaca_timestamp(bar["t"])
        session_date, is_pre, is_reg, is_aft = classify_session(ts)
        rows.append(
            (
                symbol,
                ts.astimezone(timezone.utc).replace(tzinfo=None),
                session_date,
                is_pre,
                is_reg,
                is_aft,
                float(bar["o"]),
                float(bar["h"]),
                float(bar["l"]),
                float(bar["c"]),
                int(bar["v"]),
                float(bar.get("vw") or bar["c"]),
                None,  # spread_pct (not available from bars endpoint)
                False,  # halt_status
                source_provider,
                1.0,  # quality_score: real exchange data
            )
        )
    if not rows:
        return 0
    con.executemany(
        """
        INSERT INTO minute_bars (
            symbol, timestamp, session_date,
            is_premarket, is_regular_hours, is_afterhours,
            open, high, low, close, volume, vwap,
            spread_pct, halt_status, source_provider, quality_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (symbol, timestamp) DO UPDATE SET
            session_date = EXCLUDED.session_date,
            is_premarket = EXCLUDED.is_premarket,
            is_regular_hours = EXCLUDED.is_regular_hours,
            is_afterhours = EXCLUDED.is_afterhours,
            open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
            close = EXCLUDED.close, volume = EXCLUDED.volume, vwap = EXCLUDED.vwap,
            spread_pct = EXCLUDED.spread_pct, halt_status = EXCLUDED.halt_status,
            source_provider = EXCLUDED.source_provider,
            quality_score = EXCLUDED.quality_score
        """,
        rows,
    )
    return len(rows)


def refresh_rolling_volume(con, symbol: str | None = None) -> int:
    """(Re)compute daily_bars.rolling_avg_volume_20d from raw daily volume.

    The column was NULL for every row ever ingested (upsert_daily_bars never set it),
    which silently weakened everything downstream: scan_gappers RVOL, the quality
    gate's relative_volume component, and the labeler baselines all fell back to
    within-session proxies. Value = average of the PRIOR 20 sessions (excluding the
    row itself, so it's knowable at that day's open), requires >=5 prior sessions.
    Called per-symbol after each upsert so new rows stay populated; symbol=None
    backfills the whole table (one-off)."""
    scope = "AND d.symbol = ?" if symbol else ""
    params = [symbol] if symbol else []
    cur = con.execute(
        f"""
        UPDATE daily_bars d SET rolling_avg_volume_20d = s.avg20
        FROM (
            SELECT symbol, trade_date,
                   AVG(volume) OVER w AS avg20,
                   COUNT(volume) OVER w AS cnt
            FROM daily_bars
            WINDOW w AS (PARTITION BY symbol ORDER BY trade_date
                         ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING)
        ) s
        WHERE d.symbol = s.symbol AND d.trade_date = s.trade_date
          AND s.cnt >= 5 {scope}
        """,
        params,
    )
    return getattr(cur, "rowcount", 0) or 0


def upsert_daily_bars(con, symbol: str, bars: list[dict]) -> int:
    """Insert/replace Alpaca daily bars, deriving previous_close per row."""
    ordered = sorted(bars, key=lambda b: b["t"])
    rows = []
    prev_close: float | None = None
    for bar in ordered:
        ts = parse_alpaca_timestamp(bar["t"]).astimezone(EASTERN)
        close = float(bar["c"])
        rows.append(
            (
                symbol,
                ts.date(),
                float(bar["o"]),
                float(bar["h"]),
                float(bar["l"]),
                close,
                int(bar["v"]),
                float(bar.get("vw") or close),
                prev_close,
            )
        )
        prev_close = close
    if not rows:
        return 0
    con.executemany(
        """
        INSERT INTO daily_bars (
            symbol, trade_date, open, high, low, close, volume, vwap,
            previous_close
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (symbol, trade_date) DO UPDATE SET
            open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
            close = EXCLUDED.close, volume = EXCLUDED.volume, vwap = EXCLUDED.vwap,
            previous_close = EXCLUDED.previous_close
        """,
        rows,
    )
    # keep the 20d rolling volume live for this symbol (see refresh_rolling_volume)
    try:
        refresh_rolling_volume(con, symbol)
    except Exception:  # noqa: BLE001 — never let a stats refresh break ingestion
        pass
    return len(rows)


def ingest_live_minute_bars(
    con,
    client,
    symbols: list[str],
    lookback_minutes: int = 240,
    feed: str | None = None,
) -> IngestionResult:
    """Pull recent 1-minute bars for `symbols` into the research database.

    `client` is an AlpacaPaperClient (or anything with get_minute_bars).
    Free Alpaca keys can only see IEX data, and the SIP feed additionally
    embargoes the most recent 15 minutes; we end the window slightly in the
    past so the request never 403s.
    """
    result = IngestionResult()
    if not symbols:
        return result
    result.per_symbol = {s: 0 for s in symbols}
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=lookback_minutes)
    end = now - timedelta(seconds=30)
    try:
        bars_by_symbol = client.get_minute_bars(
            symbols,
            start_iso=start.isoformat().replace("+00:00", "Z"),
            end_iso=end.isoformat().replace("+00:00", "Z"),
            feed=feed,
        )
    except Exception as exc:  # noqa: BLE001 - report, don't crash the loop
        result.errors.append(f"minute bars fetch failed: {exc}")
        return result
    for symbol, bars in bars_by_symbol.items():
        try:
            count = upsert_minute_bars(con, symbol, bars)
            result.per_symbol[symbol] = count
            if count:
                result.minute_rows += count
                result.symbols.append(symbol)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{symbol}: minute upsert failed: {exc}")
    return result


def ingest_daily_history(
    con,
    client,
    symbols: list[str],
    days: int = 30,
) -> IngestionResult:
    """Backfill daily bars so gap %% / relative volume have a baseline."""
    result = IngestionResult()
    if not symbols:
        return result
    start = (datetime.now(timezone.utc) - timedelta(days=days * 2)).date()
    try:
        bars_by_symbol = client.get_daily_bars(
            symbols, start_iso=f"{start.isoformat()}T00:00:00Z"
        )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"daily bars fetch failed: {exc}")
        return result
    for symbol, bars in bars_by_symbol.items():
        try:
            count = upsert_daily_bars(con, symbol, bars)
            if count:
                result.daily_rows += count
                result.symbols.append(symbol)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{symbol}: daily upsert failed: {exc}")
    return result


def discover_active_symbols(
    client,
    top: int = 20,
    price_min: float = 1.0,
    price_max: float = 20.0,
) -> list[str]:
    """Use Alpaca's most-actives screener to find live momentum candidates.

    Filters the screener output to the strategy's price band using latest
    trades. Failures return an empty list so the caller can fall back to a
    static symbol list.
    """
    try:
        # WILDCARD movers first: top %-GAINERS regardless of absolute share volume.
        # A thin name squeezing on a headline (CX 2026-07-02: $3->$6, the source
        # trader's entire day) never cracks a volume-ranked top-N — this is the feed
        # that sees it. Discovery/ingestion only; every trading gate still applies.
        gainers: list[str] = []
        try:
            for g in client.get_movers(top=15):
                sym = (g.get("symbol") or "").strip()
                # drop warrants/units/rights noise (BKSY.WS, EVLVW-style)
                if not sym or "." in sym or (len(sym) >= 5 and sym.endswith("W")):
                    continue
                gainers.append(sym)
        except Exception:  # noqa: BLE001 — movers is additive; actives still work
            pass
        # Alpaca caps the most-actives `top` at 100; never request more.
        actives = client.get_most_actives(top=min(top * 3, 100), by="volume")
        symbols = list(dict.fromkeys(
            gainers + [a["symbol"] for a in actives if a.get("symbol")]))
        if not symbols:
            return []
        trades = client.get_latest_trades(symbols)  # batched internally; price them all
        in_band = []
        for symbol in symbols:
            trade = trades.get(symbol)
            if not trade:
                continue
            price = float(trade.get("p") or 0.0)
            if price_min <= price <= price_max:
                in_band.append(symbol)
            if len(in_band) >= top:
                break
        return in_band
    except Exception as exc:  # noqa: BLE001 - screener needs a paid feed sometimes
        status = getattr(exc, "status", None)
        if status in (401, 403):
            logger.error(
                "most-actives screener AUTH/ENTITLEMENT failure (HTTP %s) — check "
                "ALPACA keys / data plan; this is NOT 'no movers': %s", status, exc)
        else:
            logger.warning("most-actives screener failed (HTTP %s): %s", status, exc)
        return []
