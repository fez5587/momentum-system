"""Minimal Alpaca REST client (stdlib only — no SDK dependency).

Covers what the system needs: account, positions, orders, order submission
and cancellation, minute bars, latest trades, and the most-actives screener.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from alpaca_paper.settings import AlpacaPaperSettings

logger = logging.getLogger(__name__)


def _install_dns_cache() -> None:
    """Cache DNS resolution process-wide (the real fix for the flaky-DNS host).

    The WSL host resolves the Alpaca hostnames slowly/intermittently (~5s on ~1
    in 5 lookups). urllib opens a fresh connection — and re-resolves — on EVERY
    request, so across ~20 calls/pass this stretched loop passes to 60-120s.
    Caching successful lookups for a few minutes pays the slow resolve at most
    once per host. Opt out with ALPACA_DNS_CACHE=0.
    """
    import os
    import socket
    import time as _t
    if os.environ.get("ALPACA_DNS_CACHE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    if getattr(socket.getaddrinfo, "_is_cached", False):
        return
    orig = socket.getaddrinfo
    ttl = float(os.environ.get("ALPACA_DNS_TTL", "300"))
    cache: dict = {}

    def cached_getaddrinfo(host, *args, **kwargs):
        key = (host, args, tuple(sorted(kwargs.items())))
        now = _t.monotonic()
        hit = cache.get(key)
        if hit and now - hit[1] < ttl:
            return hit[0]
        res = orig(host, *args, **kwargs)
        cache[key] = (res, now)
        return res

    cached_getaddrinfo._is_cached = True  # type: ignore[attr-defined]
    socket.getaddrinfo = cached_getaddrinfo


_install_dns_cache()


class AlpacaApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Alpaca API error {status}: {body[:300]}")
        self.status = status
        self.body = body


class AlpacaPaperClient:
    def __init__(self, settings: AlpacaPaperSettings | None = None, timeout: int = 15,
                 max_retries: int = 2):
        self.settings = settings or AlpacaPaperSettings.from_env()
        self.timeout = timeout
        # retry transient network/DNS errors (not HTTP 4xx/5xx) a couple times;
        # DNS failures fail fast, so this smooths brief blips without much latency
        self.max_retries = max_retries
        # tiny TTL cache for hot read-only endpoints (account/positions). The
        # breaker, sync, guard and exit manager each read these every pass; on a
        # flaky-DNS host every call risks a multi-second resolve, so collapsing
        # the redundant reads within a pass is the difference between ~2s and
        # ~60s loop passes. Short TTL keeps risk checks effectively current.
        self._cache: dict = {}
        self._cache_lock = threading.Lock()
        self.cache_ttl = 2.5

    def _cached(self, key: str, fn):
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and (now - hit[1]) < self.cache_ttl:
                return hit[0]
        val = fn()  # network call OUTSIDE the lock
        with self._cache_lock:
            self._cache[key] = (val, time.monotonic())
        return val

    def _invalidate_cache(self):
        with self._cache_lock:
            self._cache.clear()

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
        import time as _time
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                # 429 (rate limit) and 5xx (server) are TRANSIENT — retry with
                # backoff, honoring Retry-After. Previously these hard-failed,
                # which fed silent-stale ingest (a rate-limited bar fetch looked
                # like a clean "0 rows" pass). Other 4xx are real — surface them.
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = float(retry_after) if retry_after else 0.0
                    except (TypeError, ValueError):
                        delay = 0.0
                    _time.sleep(max(delay, 0.5 * (2 ** attempt)))  # exp backoff floor
                    continue
                raise AlpacaApiError(exc.code, exc.read().decode(errors="replace")) from exc
            except urllib.error.URLError:
                # transient network/DNS (e.g. gaierror) — retry a couple times
                if attempt >= self.max_retries:
                    raise
                _time.sleep(0.4 * (attempt + 1))
        raise RuntimeError("request retries exhausted")  # unreachable

    def _trading(self, method: str, path: str, **kw):
        return self._request(
            method, f"{self.settings.trading_base_url}/v2{path}", **kw
        )

    def _data(self, method: str, path: str, **kw):
        return self._request(method, f"{self.settings.data_base_url}{path}", **kw)

    @staticmethod
    def _chunked(items: list[str], size: int = 100):
        """Yield symbol batches; Alpaca caps multi-symbol requests (~100)."""
        for i in range(0, len(items), size):
            yield items[i : i + size]

    # -- trading API -------------------------------------------------------

    def get_account(self) -> dict:
        return self._cached("account", lambda: self._trading("GET", "/account"))

    def get_clock(self) -> dict:
        """Broker market clock: {is_open, next_open, next_close, timestamp}. The
        authoritative open/closed check (handles weekends, holidays, half-days)."""
        return self._trading("GET", "/clock")

    def get_positions(self, fresh: bool = False) -> list[dict]:
        # fresh=True bypasses the TTL cache — REQUIRED for the naked-stop guard,
        # which must never decide an order is unfilled off a stale snapshot.
        if fresh:
            val = self._trading("GET", "/positions")
            with self._cache_lock:
                self._cache["positions"] = (val, time.monotonic())
            return val
        return self._cached("positions", lambda: self._trading("GET", "/positions"))

    def get_orders(
        self, status: str = "all", limit: int = 500, nested: bool = True,
        symbols: list[str] | None = None,
    ) -> list[dict]:
        # nested=true includes bracket child legs (stop/target) so working risk
        # isn't under-reported in the orders snapshot.
        #
        # IMPORTANT: Alpaca's `limit` truncates to the most-RECENT orders (and
        # counts child legs toward the cap). On a busy day the order list runs to
        # hundreds (every backed-out entry is an order), so a small limit silently
        # drops the stop legs of older OPEN positions — a stop-leg lookup then
        # reads "no stop" on a fully-protected position. Pass `symbols` to scope
        # the query to the handful of held names (no truncation risk); that's what
        # the exit manager / safety checks must do.
        params: dict = {"status": status, "limit": min(limit, 500),
                        "nested": str(nested).lower(), "direction": "desc"}
        if symbols:
            # scoped to a few names — well under the cap, one page is complete
            params["symbols"] = ",".join(symbols)
            return self._trading("GET", "/orders", params=params)
        # unscoped: page backwards with `until` so the whole history is captured,
        # not just the most-recent page (the journal needs every fill, incl. early
        # ones, or realized P&L silently drops the morning's round-trips). Dedup by
        # id across page boundaries; cap pages as a runaway guard.
        out: list[dict] = []
        seen: set = set()
        until: str | None = None
        for _ in range(20):
            page_params = dict(params)
            if until:
                page_params["until"] = until
            page = self._trading("GET", "/orders", params=page_params) or []
            fresh = [o for o in page if o.get("id") not in seen]
            for o in fresh:
                seen.add(o.get("id"))
            out.extend(fresh)
            if len(page) < params["limit"]:
                break  # last page
            subs = [o.get("submitted_at") for o in page if o.get("submitted_at")]
            if not subs:
                break
            until = min(subs)  # next page: orders older than this one
        return out

    def submit_order(
        self,
        symbol: str,
        qty: int,
        side: str = "buy",
        order_type: str = "market",
        time_in_force: str = "day",
        limit_price: float | None = None,
        stop_price: float | None = None,
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        self._invalidate_cache()
        body: dict = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if limit_price is not None:
            body["limit_price"] = str(round(limit_price, 2))
        if stop_price is not None:          # standalone stop / stop-limit trigger
            body["stop_price"] = str(round(stop_price, 2))
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
        self._invalidate_cache()
        self._trading("DELETE", f"/orders/{order_id}")

    def replace_order(
        self, order_id: str, stop_price: float | None = None,
        limit_price: float | None = None, qty: int | None = None,
    ) -> dict:
        """PATCH an open order — e.g. move a bracket STOP leg up to breakeven or
        trail it — WITHOUT cancelling it. Cancelling a bracket leg tears down the
        OCO and leaves the position naked (the bug we already fixed once); a
        replace keeps the protection intact and just changes the price."""
        self._invalidate_cache()
        body: dict = {}
        if stop_price is not None:
            body["stop_price"] = str(round(stop_price, 2))
        if limit_price is not None:
            body["limit_price"] = str(round(limit_price, 2))
        if qty is not None:
            body["qty"] = str(int(qty))
        if not body:
            return {}
        return self._trading("PATCH", f"/orders/{order_id}", body=body)

    def close_position(self, symbol: str, qty: int | None = None,
                       percentage: float | None = None) -> dict:
        self._invalidate_cache()
        params = {}
        if qty is not None:
            params["qty"] = str(int(qty))
        elif percentage is not None:
            params["percentage"] = str(round(percentage, 4))
        return self._trading("DELETE", f"/positions/{symbol}",
                             params=params or None)

    # -- market data API ----------------------------------------------------

    def get_minute_bars(
        self,
        symbols: list[str],
        start_iso: str,
        end_iso: str | None = None,
        limit: int = 10_000,
        feed: str | None = None,
    ) -> dict[str, list[dict]]:
        """Fetch 1-minute bars, batching symbols (cap-safe) + following pagination."""
        all_bars: dict[str, list[dict]] = {s: [] for s in symbols}
        for chunk in self._chunked(symbols, 100):
            page_token = None
            while True:
                params = {
                    "symbols": ",".join(chunk),
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
        trades: dict[str, dict] = {}
        for chunk in self._chunked(symbols, 100):
            payload = self._data(
                "GET",
                "/v2/stocks/trades/latest",
                params={"symbols": ",".join(chunk), "feed": self.settings.feed},
            )
            trades.update(payload.get("trades") or {})
        return trades

    def get_latest_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Latest NBBO quote per symbol (bid/ask). Mirrors ``get_latest_trades``;
        each quote dict carries Alpaca's raw keys — ``bp``/``ap`` (bid/ask price)
        and ``bs``/``as`` (sizes). On the free IEX feed quotes are thinner than
        SIP, so a symbol may be absent; callers must treat a missing entry as
        "unknown spread", not a tight one."""
        quotes: dict[str, dict] = {}
        for chunk in self._chunked(symbols, 100):
            payload = self._data(
                "GET",
                "/v2/stocks/quotes/latest",
                params={"symbols": ",".join(chunk), "feed": self.settings.feed},
            )
            quotes.update(payload.get("quotes") or {})
        return quotes

    def get_most_actives(self, top: int = 20, by: str = "volume") -> list[dict]:
        payload = self._data(
            "GET",
            "/v1beta1/screener/stocks/most-actives",
            params={"top": top, "by": by},
        )
        return payload.get("most_actives") or []

    def get_news(
        self,
        symbols: list[str] | None = None,
        limit: int = 50,
        start_iso: str | None = None,
        sort: str = "desc",
    ) -> list[dict]:
        """Latest market news (Benzinga). Each item carries a stable ``id`` and an
        authoritative ``symbols`` list, so no regex ticker-scraping is needed.
        Optional symbol filter (capped at 50 symbols/req); Alpaca caps ``limit`` at 50."""
        params: dict = {
            "limit": max(1, min(int(limit), 50)),
            "sort": sort,
            "exclude_contentless": "true",
        }
        if symbols:
            params["symbols"] = ",".join(symbols[:50])
        if start_iso:
            params["start"] = start_iso
        payload = self._data("GET", "/v1beta1/news", params=params)
        return payload.get("news") or []

    def get_daily_bars(
        self, symbols: list[str], start_iso: str, end_iso: str | None = None
    ) -> dict[str, list[dict]]:
        """Daily bars, batching symbols (cap-safe) + following pagination."""
        all_bars: dict[str, list[dict]] = {}
        for chunk in self._chunked(symbols, 100):
            page_token = None
            while True:
                payload = self._data(
                    "GET",
                    "/v2/stocks/bars",
                    params={
                        "symbols": ",".join(chunk),
                        "timeframe": "1Day",
                        "start": start_iso,
                        "end": end_iso,
                        "limit": 10_000,
                        "feed": self.settings.feed,
                        "adjustment": "raw",
                        "page_token": page_token,
                    },
                )
                for symbol, bars in (payload.get("bars") or {}).items():
                    all_bars.setdefault(symbol, []).extend(bars or [])
                page_token = payload.get("next_page_token")
                if not page_token:
                    break
        return all_bars
