"""Sync Schwab account state into the event store."""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime

from schwab.positions.reader import PositionsReader
from storage.event_schema import (
    AccountPositionsUpdatedEvent,
    AccountSummaryUpdatedEvent,
    EventMode,
)
from storage.event_store import EventStore

logger = logging.getLogger(__name__)

BROKER_NAME = "schwab"


class AccountSync:
    def __init__(
        self,
        store: EventStore,
        reader: PositionsReader | None = None,
        session_id: str | None = None,
        mode: EventMode = EventMode.LIVE,
    ):
        self.store = store
        self.reader = reader or PositionsReader()
        self.session_id = session_id
        self.mode = mode

    def sync_summary(self) -> dict:
        summary = self.reader.read_account_summary()
        self.store.emit(
            AccountSummaryUpdatedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=f"Schwab account {summary.account_id} equity {summary.total_equity}",
                broker_name=BROKER_NAME,
                account_id=summary.account_id,
                account_desc=summary.account_desc,
                total_equity=summary.total_equity,
                cash_balance=summary.cash_balance,
                buying_power=summary.buying_power,
                net_liquidating_value=summary.net_liquidating_value,
            )
        )
        return asdict(summary)

    def sync_positions(self) -> dict:
        snapshot = self.reader.read_positions()
        self.store.emit(
            AccountPositionsUpdatedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=f"Schwab positions: {len(snapshot.positions)}",
                broker_name=BROKER_NAME,
                account_id=snapshot.account_id,
                positions=[asdict(p) for p in snapshot.positions],
            )
        )
        return asdict(snapshot)

    def sync_all(self) -> dict:
        return {"summary": self.sync_summary(), "positions": self.sync_positions()}
