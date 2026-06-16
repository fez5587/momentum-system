"""Adapter: Schwab market payloads -> typed models / DataFrames."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from schwab.market.models import PriceHistory, Quote


def quotes_from_payload(payload: dict) -> list[Quote]:
    return [Quote.from_api(symbol, body) for symbol, body in payload.items()]


def price_history_to_dataframe(history: PriceHistory) -> pd.DataFrame:
    rows = [
        {
            "timestamp": datetime.fromtimestamp(
                c.datetime_ms / 1000, tz=timezone.utc
            ).replace(tzinfo=None),
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
        }
        for c in history.candles
    ]
    return pd.DataFrame(rows)


def payload_to_dataframe(payload: dict) -> pd.DataFrame:
    return price_history_to_dataframe(PriceHistory.from_api(payload))
