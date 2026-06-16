"""Read Schwab account summary and positions.

When no valid token exists (the common case before OAuth setup), the reader
returns clearly-labelled fallback data rather than crashing, so the rest of
the system — and its tests — keep working.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from schwab.auth.lifecycle import TokenLifecycle
from schwab.positions.models import AccountPositions, AccountSummary, Position
from schwab.settings import SchwabSettings

logger = logging.getLogger(__name__)


class PositionsReader:
    def __init__(
        self,
        settings: SchwabSettings | None = None,
        lifecycle: TokenLifecycle | None = None,
        timeout: int = 15,
    ):
        self.settings = settings or SchwabSettings.from_env()
        self.lifecycle = lifecycle or TokenLifecycle(self.settings)
        self.timeout = timeout

    def _get(self, path: str, token: str, params: dict | None = None):
        url = f"{self.settings.api_base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode() or "{}")

    # -- fallbacks -------------------------------------------------------

    @staticmethod
    def _fallback_account_summary() -> AccountSummary:
        return AccountSummary(
            account_id="SCHWAB-UNAUTH",
            account_desc="Schwab (not authenticated — fallback data)",
            is_fallback=True,
        )

    @staticmethod
    def _fallback_positions() -> AccountPositions:
        return AccountPositions(account_id="SCHWAB-UNAUTH", is_fallback=True)

    # -- public ----------------------------------------------------------

    def read_account_summary(self) -> AccountSummary:
        token = self.lifecycle.get_access_token()
        if not token:
            return self._fallback_account_summary()
        try:
            accounts = self._get("/trader/v1/accounts", token)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            logger.exception("schwab account read failed")
            return self._fallback_account_summary()
        if not accounts:
            return self._fallback_account_summary()
        account = accounts[0].get("securitiesAccount", accounts[0])
        balances = account.get("currentBalances", {})
        return AccountSummary(
            account_id=str(account.get("accountNumber") or "schwab"),
            account_desc=account.get("type", "Schwab"),
            total_equity=float(balances.get("equity") or 0),
            cash_balance=float(balances.get("cashBalance") or 0),
            buying_power=float(balances.get("buyingPower") or 0),
            net_liquidating_value=float(balances.get("liquidationValue") or 0),
        )

    def read_positions(self) -> AccountPositions:
        token = self.lifecycle.get_access_token()
        if not token:
            return self._fallback_positions()
        try:
            accounts = self._get(
                "/trader/v1/accounts", token, params={"fields": "positions"}
            )
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            logger.exception("schwab positions read failed")
            return self._fallback_positions()
        if not accounts:
            return self._fallback_positions()
        account = accounts[0].get("securitiesAccount", accounts[0])
        positions = [
            Position(
                symbol=p.get("instrument", {}).get("symbol", ""),
                quantity=float(p.get("longQuantity") or 0)
                - float(p.get("shortQuantity") or 0),
                avg_entry_price=float(p.get("averagePrice") or 0),
                market_value=float(p.get("marketValue") or 0),
                unrealized_pnl=float(p.get("longOpenProfitLoss") or 0),
                asset_type=p.get("instrument", {}).get("assetType", "EQUITY"),
            )
            for p in account.get("positions") or []
        ]
        return AccountPositions(
            account_id=str(account.get("accountNumber") or "schwab"),
            positions=positions,
        )
