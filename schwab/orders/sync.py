"""Sync Schwab orders into the event store."""

from __future__ import annotations

import logging
from datetime import datetime

from schwab.orders.reader import OrdersReader
from storage.event_schema import AccountOrdersUpdatedEvent, EventMode
from storage.event_store import EventStore

logger = logging.getLogger(__name__)

BROKER_NAME = "schwab"


class OrderSync:
    def __init__(
        self,
        store: EventStore,
        reader: OrdersReader | None = None,
        session_id: str | None = None,
        mode: EventMode = EventMode.LIVE,
        account_id: str = "schwab",
    ):
        self.store = store
        self.reader = reader or OrdersReader()
        self.session_id = session_id
        self.mode = mode
        self.account_id = account_id

    def sync_orders(self) -> list[dict]:
        orders = self.reader.read_orders()
        self.store.emit(
            AccountOrdersUpdatedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=f"Schwab orders: {len(orders)}",
                broker_name=BROKER_NAME,
                account_id=self.account_id,
                orders=orders,
            )
        )
        return orders
