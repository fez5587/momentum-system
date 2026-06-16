"""Read Schwab orders (read-only, with unauthenticated fallback)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from schwab.auth.lifecycle import TokenLifecycle
from schwab.settings import SchwabSettings

logger = logging.getLogger(__name__)


class OrdersReader:
    def __init__(
        self,
        settings: SchwabSettings | None = None,
        lifecycle: TokenLifecycle | None = None,
        timeout: int = 15,
    ):
        self.settings = settings or SchwabSettings.from_env()
        self.lifecycle = lifecycle or TokenLifecycle(self.settings)
        self.timeout = timeout

    def read_orders(self, days_back: int = 2) -> list[dict]:
        token = self.lifecycle.get_access_token()
        if not token:
            return []
        now = datetime.now(timezone.utc)
        params = urllib.parse.urlencode(
            {
                "fromEnteredTime": (now - timedelta(days=days_back)).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                ),
                "toEnteredTime": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            }
        )
        url = f"{self.settings.api_base_url}/trader/v1/orders?{params}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = json.loads(resp.read().decode() or "[]")
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            logger.exception("schwab orders read failed")
            return []
        return [
            {
                "broker_order_id": str(o.get("orderId") or ""),
                "symbol": (o.get("orderLegCollection") or [{}])[0]
                .get("instrument", {})
                .get("symbol"),
                "side": (o.get("orderLegCollection") or [{}])[0].get("instruction"),
                "quantity": o.get("quantity"),
                "filled_quantity": o.get("filledQuantity"),
                "status": o.get("status"),
                "type": o.get("orderType"),
                "price": o.get("price"),
                "submitted_at": o.get("enteredTime"),
            }
            for o in raw
        ]
