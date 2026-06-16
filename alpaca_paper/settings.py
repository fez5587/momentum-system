"""Alpaca paper-trading settings."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AlpacaPaperSettings:
    api_key: str = ""
    secret_key: str = ""
    trading_base_url: str = "https://paper-api.alpaca.markets"
    data_base_url: str = "https://data.alpaca.markets"
    feed: str = "iex"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AlpacaPaperSettings":
        values = dict(os.environ)
        if env is not None:
            values.update(env)
        api_key = values.get("ALPACA_API_KEY") or values.get("APCA_API_KEY_ID") or ""
        secret = (
            values.get("ALPACA_SECRET_KEY")
            or values.get("APCA_API_SECRET_KEY")
            or ""
        )
        base = values.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
        feed = values.get("ALPACA_DATA_FEED", "iex")
        return cls(
            api_key=api_key,
            secret_key=secret,
            trading_base_url=base.rstrip("/"),
            feed=feed,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.secret_key)
