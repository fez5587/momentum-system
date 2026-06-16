"""Sync the Alpaca paper account into the event store.

Emits account_summary_updated / account_positions_updated /
account_orders_updated events that the dashboard projections read.
"""

from __future__ import annotations

import logging
from datetime import datetime

from alpaca_paper.client import AlpacaPaperClient
from storage.event_schema import (
    AccountOrdersUpdatedEvent,
    AccountPositionsUpdatedEvent,
    AccountSummaryUpdatedEvent,
    EventMode,
)
from storage.event_store import EventStore

logger = logging.getLogger(__name__)

BROKER_NAME = "alpaca_paper"


class AlpacaPaperSync:
    def __init__(
        self,
        store: EventStore,
        client: AlpacaPaperClient | None = None,
        session_id: str | None = None,
        mode: EventMode = EventMode.PAPER,
    ):
        self.store = store
        self.client = client or AlpacaPaperClient()
        self.session_id = session_id
        self.mode = mode

    def sync_account(self) -> dict | None:
        try:
            account = self.client.get_account()
        except Exception:
            logger.exception("alpaca account sync failed")
            return None
        account_id = str(account.get("account_number") or account.get("id") or "paper")
        self.store.emit(
            AccountSummaryUpdatedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=f"Alpaca paper equity {account.get('equity')}",
                broker_name=BROKER_NAME,
                account_id=account_id,
                account_desc="Alpaca Paper",
                total_equity=float(account.get("equity") or 0),
                cash_balance=float(account.get("cash") or 0),
                buying_power=float(account.get("buying_power") or 0),
                net_liquidating_value=float(account.get("equity") or 0),
            )
        )
        return account

    def sync_positions(self) -> list[dict] | None:
        try:
            raw = self.client.get_positions()
        except Exception:
            logger.exception("alpaca positions sync failed")
            return None
        positions = [
            {
                "symbol": p.get("symbol"),
                "quantity": float(p.get("qty") or 0),
                "avg_entry_price": float(p.get("avg_entry_price") or 0),
                "current_price": float(p.get("current_price") or 0),
                "market_value": float(p.get("market_value") or 0),
                "unrealized_pnl": float(p.get("unrealized_pl") or 0),
                "unrealized_pnl_pct": float(p.get("unrealized_plpc") or 0),
                "side": p.get("side"),
            }
            for p in raw
        ]
        self.store.emit(
            AccountPositionsUpdatedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=f"Alpaca paper positions: {len(positions)}",
                broker_name=BROKER_NAME,
                account_id="paper",
                positions=positions,
            )
        )
        return positions

    def sync_orders(self, status: str = "all", limit: int = 50) -> list[dict] | None:
        try:
            raw = self.client.get_orders(status=status, limit=limit)
        except Exception:
            logger.exception("alpaca orders sync failed")
            return None
        orders = [
            {
                "broker_order_id": o.get("id"),
                "client_order_id": o.get("client_order_id"),
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "quantity": float(o.get("qty") or 0),
                "filled_quantity": float(o.get("filled_qty") or 0),
                "type": o.get("type"),
                "status": o.get("status"),
                "limit_price": o.get("limit_price"),
                "filled_avg_price": o.get("filled_avg_price"),
                "submitted_at": o.get("submitted_at"),
            }
            for o in raw
        ]
        self.store.emit(
            AccountOrdersUpdatedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=f"Alpaca paper orders: {len(orders)}",
                broker_name=BROKER_NAME,
                account_id="paper",
                orders=orders,
            )
        )
        return orders

    def sync_all(self) -> dict:
        return {
            "account": self.sync_account(),
            "positions": self.sync_positions(),
            "orders": self.sync_orders(),
        }
