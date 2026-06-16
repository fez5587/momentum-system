"""Minimal Alpaca REST client (stdlib only — no SDK dependency).

Covers what the system needs: account, positions, orders, order submission
and cancellation, minute bars, latest trades, and the most-actives screener.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from alpaca_paper.settings import AlpacaPaperSettings

logger = logging.getLogger(__name__)


class AlpacaApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Alpaca API error {status}: {body[:300]}")
        self.status = status
        self.body = body


class AlpacaPaperClient:
    def __init__(self, settings: AlpacaPaperSettings | None = None, timeout: int = 15):
        self.settings = settings or AlpacaPaperSettings.from_env()
        self.timeout = timeout

    # -- low level -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.settings.api_key,
            "APCA-API-SECRET-KEY": self.settings.secret_key,
            "Content-Type": "application/json",
        }

    def _request(
        self, method: str, url: str, params: dict | None = None, body: dict | None = None
    ):
        if params:
            qs = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
            url = f"{url}?{qs}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, headers=self._headers(), method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise AlpacaApiError(exc.code, exc.read().decode(errors="replace")) from exc

    def _trading(self, method: str, path: str, **kw):
        return self._request(
            method, f"{self.settings.trading_base_url}/v2{path}", **kw
        )

    def _data(self, method: str, path: str, **kw):
        return self._request(method, f"{self.settings.data_base_url}{path}", **kw)

    # -- trading API -------------------------------------------------------

    def get_account(self) -> dict:
        return self._trading("GET", "/account")

    def get_positions(self) -> list[dict]:
        return self._trading("GET", "/positions")

    def get_orders(self, status: str = "all", limit: int = 100) -> list[dict]:
        return self._trading(
            "GET", "/orders", params={"status": status, "limit": limit}
        )

    def submit_order(
        self,
        symbol: str,
        qty: int,
        side: str = "buy",
        order_type: str = "market",
        time_in_force: str = "day",
        limit_price: float | None = None,
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        body: dict = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if limit_price is not None:
            body["limit_price"] = str(round(limit_price, 2))
        if client_order_id:
            body["client_order_id"] = client_order_id
        if stop_loss_price is not None or take_profit_price is not None:
            body["order_class"] = "bracket" if (
                stop_loss_price and take_profit_price
            ) else "oto"
            if stop_loss_price is not None:
                body["stop_loss"] = {"stop_price": str(round(stop_loss_price, 2))}
            if take_profit_price is not None:
                body["take_profit"] = {"limit_price": str(round(take_profit_price, 2))}
        return self._trading("POST", "/orders", body=body)

    def cancel_order(self, order_id: str) -> None:
        self._trading("DELETE", f"/orders/{order_id}")

    def close_position(self, symbol: str) -> dict:
        return self._trading("DELETE", f"/positions/{symbol}")

    # -- market data API ----------------------------------------------------

    def get_minute_bars(
        self,
        symbols: list[str],
        start_iso: str,
        end_iso: str | None = None,
        limit: int = 10_000,
        feed: str | None = None,
    ) -> dict[str, list[dict]]:
        """Fetch 1-minute bars for symbols, following pagination tokens."""
        all_bars: dict[str, list[dict]] = {s: [] for s in symbols}
        page_token = None
        while True:
            params = {
                "symbols": ",".join(symbols),
                "timeframe": "1Min",
                "start": start_iso,
                "end": end_iso,
                "limit": limit,
                "feed": feed or self.settings.feed,
                "adjustment": "raw",
                "page_token": page_token,
            }
            payload = self._data("GET", "/v2/stocks/bars", params=params)
            for symbol, bars in (payload.get("bars") or {}).items():
                all_bars.setdefault(symbol, []).extend(bars or [])
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        return all_bars

    def get_latest_trades(self, symbols: list[str]) -> dict[str, dict]:
        payload = self._data(
            "GET",
            "/v2/stocks/trades/latest",
            params={"symbols": ",".join(symbols), "feed": self.settings.feed},
        )
        return payload.get("trades") or {}

    def get_most_actives(self, top: int = 20, by: str = "volume") -> list[dict]:
        payload = self._data(
            "GET",
            "/v1beta1/screener/stocks/most-actives",
            params={"top": top, "by": by},
        )
        return payload.get("most_actives") or []

    def get_daily_bars(
        self, symbols: list[str], start_iso: str, end_iso: str | None = None
    ) -> dict[str, list[dict]]:
        payload = self._data(
            "GET",
            "/v2/stocks/bars",
            params={
                "symbols": ",".join(symbols),
                "timeframe": "1Day",
                "start": start_iso,
                "end": end_iso,
                "limit": 10_000,
                "feed": self.settings.feed,
                "adjustment": "raw",
            },
        )
        return payload.get("bars") or {}
