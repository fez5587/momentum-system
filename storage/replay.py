import json

"""Replay/rebuild tooling for Milestone 2.

Reconstruct state from event store for analysis and testing.
"""

import logging
from typing import TYPE_CHECKING

from .event_store import EventStore

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class StateRebuilder:
    """Rebuild trading state from event store."""

    def __init__(self, store: EventStore):
        self.store = store

    def rebuild_session(self, session_id: str) -> dict:
        """Rebuild complete state for a session from events."""
        logger.info(f"Rebuilding session {session_id}")

        events = self.store.query_events(session_id=session_id)

        state = {
            "session_id": session_id,
            "symbols": {},
            "orders": {},
            "positions": {},
            "events": [],
        }

        for event in events:
            event_type = event["event_type"]
            payload = json.loads(event.get("payload_json", "{}"))
            symbol = payload.get("symbol")

            self._process_event(state, event_type, symbol, payload, event)

        logger.info(f"Rebuilt state for {len(events)} events")
        return state

    def _process_event(
        self,
        state: dict,
        event_type: str,
        symbol: str | None,
        payload: dict,
        event: dict,
    ):
        """Process individual event and update state."""
        if event_type == "symbol_discovered":
            state["symbols"][symbol] = {
                "state": "discovered",
                "discovered_at": event["timestamp"],
            }

        elif event_type == "symbol_state_changed":
            if symbol not in state["symbols"]:
                state["symbols"][symbol] = {
                    "state": payload.get("previous_state") or "discovered",
                    "discovered_at": event["timestamp"],
                }
            state["symbols"][symbol]["state"] = payload["new_state"]
            state["symbols"][symbol]["state_history"] = state["symbols"][symbol].get(
                "state_history", []
            )
            state["symbols"][symbol]["state_history"].append(
                {
                    "state": payload["new_state"],
                    "timestamp": event["timestamp"],
                    "reason": payload.get("state_reason"),
                }
            )

        elif event_type == "signal_ready":
            if symbol in state["symbols"]:
                state["symbols"][symbol]["signals"] = state["symbols"][symbol].get(
                    "signals", []
                )
                state["symbols"][symbol]["signals"].append(
                    {
                        "signal_type": payload["signal_type"],
                        "confidence": payload["confidence"],
                        "timestamp": event["timestamp"],
                    }
                )

        elif event_type == "position_opened":
            position_id = payload["position_id"]
            state["positions"][position_id] = {
                "symbol": symbol,
                "status": "open",
                "entry_price": payload["entry_price"],
                "quantity": payload["quantity"],
                "stop_loss_price": payload["stop_loss_price"],
                "opened_at": event["timestamp"],
            }

        elif event_type == "position_closed":
            position_id = payload["position_id"]
            if position_id in state["positions"]:
                state["positions"][position_id].update(
                    {
                        "status": "closed",
                        "exit_price": payload["exit_price"],
                        "exit_reason": payload["exit_reason"],
                        "realized_pnl": payload["realized_pnl"],
                        "closed_at": event["timestamp"],
                    }
                )

        elif event_type == "order_filled":
            state["events"].append(
                {
                    "type": event_type,
                    "order_id": payload["order_id"],
                    "symbol": symbol,
                    "timestamp": event["timestamp"],
                }
            )

        elif event_type == "order_submitted":
            order_id = payload["order_id"]
            state["orders"][order_id] = {
                "symbol": symbol,
                "status": "submitted",
                "side": payload.get("side"),
                "quantity": payload.get("quantity"),
                "price": payload.get("price"),
                "updated_at": event["timestamp"],
            }

        elif event_type == "order_cancelled":
            order_id = payload["order_id"]
            state["orders"][order_id] = {
                **state["orders"].get(order_id, {}),
                "symbol": symbol,
                "status": "cancelled",
                "cancel_reason": payload.get("cancel_reason"),
                "updated_at": event["timestamp"],
            }
