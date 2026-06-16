"""Schwab API settings (Milestone 3)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SchwabSettings:
    market_data_app_key: str = ""
    market_data_app_secret: str = ""
    broker_app_key: str = ""
    broker_app_secret: str = ""
    redirect_uri: str = "https://127.0.0.1:8182/callback"
    token_path: str = "data/schwab_tokens.json"
    api_base_url: str = "https://api.schwabapi.com"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SchwabSettings":
        values = dict(os.environ)
        if env is not None:
            values.update(env)
        return cls(
            market_data_app_key=values.get("SCHWAB_MARKET_DATA_APP_KEY", ""),
            market_data_app_secret=values.get("SCHWAB_MARKET_DATA_APP_SECRET", ""),
            broker_app_key=values.get("SCHWAB_BROKER_APP_KEY", ""),
            broker_app_secret=values.get("SCHWAB_BROKER_APP_SECRET", ""),
            redirect_uri=values.get(
                "SCHWAB_REDIRECT_URI", "https://127.0.0.1:8182/callback"
            ),
            token_path=values.get("SCHWAB_TOKEN_PATH", "data/schwab_tokens.json"),
        )

    @property
    def has_market_data_credentials(self) -> bool:
        return bool(self.market_data_app_key and self.market_data_app_secret)

    @property
    def has_broker_credentials(self) -> bool:
        return bool(self.broker_app_key and self.broker_app_secret)
