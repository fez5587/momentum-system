"""Schwab market-data models.

(Repaired: the original had broken dataclass indentation, a `string` type
annotation, and an invalid `int = None = None` default chain.)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Quote:
    symbol: str
    bid_price: float | None = None
    ask_price: float | None = None
    last_price: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    total_volume: int | None = None
    quote_time: str | None = None

    @classmethod
    def from_api(cls, symbol: str, payload: dict) -> "Quote":
        quote = payload.get("quote") or payload
        return cls(
            symbol=symbol,
            bid_price=quote.get("bidPrice"),
            ask_price=quote.get("askPrice"),
            last_price=quote.get("lastPrice"),
            bid_size=quote.get("bidSize"),
            ask_size=quote.get("askSize"),
            total_volume=quote.get("totalVolume"),
            quote_time=str(quote.get("quoteTime")) if quote.get("quoteTime") else None,
        )


@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: int
    datetime_ms: int

    @classmethod
    def from_api(cls, payload: dict) -> "Candle":
        return cls(
            open=float(payload.get("open") or 0),
            high=float(payload.get("high") or 0),
            low=float(payload.get("low") or 0),
            close=float(payload.get("close") or 0),
            volume=int(payload.get("volume") or 0),
            datetime_ms=int(payload.get("datetime") or 0),
        )


@dataclass
class PriceHistory:
    symbol: str
    candles: list[Candle] = field(default_factory=list)
    empty: bool = True

    @classmethod
    def from_api(cls, payload: dict) -> "PriceHistory":
        candles = [Candle.from_api(c) for c in payload.get("candles") or []]
        return cls(
            symbol=payload.get("symbol", ""),
            candles=candles,
            empty=payload.get("empty", not candles),
        )


@dataclass
class OptionQuote:
    symbol: str
    strike: float | None = None
    expiration: str | None = None
    put_call: str | None = None
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    delta: float | None = None
    open_interest: int | None = None
    volume: int | None = None
