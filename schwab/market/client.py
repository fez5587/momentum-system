"""Schwab market-data HTTP client (quotes + price history)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from schwab.auth.lifecycle import TokenLifecycle
from schwab.settings import SchwabSettings

logger = logging.getLogger(__name__)


class SchwabApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Schwab API error {status}: {body[:300]}")
        self.status = status
        self.body = body


class SchwabMarketClient:
    def __init__(
        self,
        settings: SchwabSettings | None = None,
        lifecycle: TokenLifecycle | None = None,
        timeout: int = 15,
    ):
        self.settings = settings or SchwabSettings.from_env()
        self.lifecycle = lifecycle or TokenLifecycle(self.settings)
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> dict:
        token = self.lifecycle.get_access_token()
        if not token:
            raise SchwabApiError(401, "no valid Schwab access token")
        url = f"{self.settings.api_base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            raise SchwabApiError(exc.code, exc.read().decode(errors="replace")) from exc

    def get_quotes(self, symbols: list[str]) -> dict:
        return self._get(
            "/marketdata/v1/quotes", params={"symbols": ",".join(symbols)}
        )

    def get_price_history(
        self,
        symbol: str,
        period_type: str = "day",
        period: int = 1,
        frequency_type: str = "minute",
        frequency: int = 1,
        need_extended_hours: bool = True,
    ) -> dict:
        return self._get(
            "/marketdata/v1/pricehistory",
            params={
                "symbol": symbol,
                "periodType": period_type,
                "period": period,
                "frequencyType": frequency_type,
                "frequency": frequency,
                "needExtendedHoursData": str(need_extended_hours).lower(),
            },
        )

    def get_option_chain(self, symbol: str, contract_type: str = "ALL") -> dict:
        return self._get(
            "/marketdata/v1/chains",
            params={"symbol": symbol, "contractType": contract_type},
        )
