"""Alpaca paper execution bridge.

Turns an approved execution request into a real Alpaca paper order and
emits order lifecycle events into the event store.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from alpaca_paper.client import AlpacaApiError, AlpacaPaperClient
from storage.event_schema import (
    EventMode,
    OrderCancelledEvent,
    OrderFilledEvent,
    OrderSubmittedEvent,
)
from storage.event_store import EventStore

logger = logging.getLogger(__name__)


@dataclass
class ExecutionRequest:
    """Broker-agnostic order request derived from a signal."""

    symbol: str
    side: str
    quantity: int
    entry_price: float | None = None
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    order_type: str = "market"
    time_in_force: str = "day"
    order_id: str = ""

    def __post_init__(self):
        if not self.order_id:
            self.order_id = str(uuid.uuid4())

    def to_payload(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "order_id": self.order_id,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "ExecutionRequest":
        return cls(
            symbol=payload["symbol"],
            side=payload.get("side", "buy"),
            quantity=int(payload.get("quantity", 0)),
            entry_price=payload.get("entry_price"),
            stop_loss_price=payload.get("stop_loss_price"),
            take_profit_price=payload.get("take_profit_price"),
            order_type=payload.get("order_type", "market"),
            time_in_force=payload.get("time_in_force", "day"),
            order_id=str(payload.get("order_id") or ""),
        )


@dataclass
class ExecutionResult:
    ok: bool
    order_id: str
    broker_order_id: str | None = None
    status: str | None = None
    error: str | None = None
    raw: dict | None = None


class AlpacaPaperExecutor:
    """Submits execution requests to the Alpaca paper account."""

    broker_name = "alpaca_paper"

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

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.quantity <= 0:
            return ExecutionResult(
                ok=False, order_id=request.order_id, error="quantity must be > 0"
            )
        try:
            raw = self.client.submit_order(
                symbol=request.symbol,
                qty=request.quantity,
                side=request.side,
                order_type=request.order_type,
                time_in_force=request.time_in_force,
                limit_price=request.entry_price
                if request.order_type == "limit"
                else None,
                stop_loss_price=request.stop_loss_price,
                take_profit_price=request.take_profit_price,
                client_order_id=request.order_id[:48],
            )
        except AlpacaApiError as exc:
            logger.error("alpaca order failed: %s", exc)
            self._emit_cancelled(request, f"broker rejected: {exc}")
            return ExecutionResult(
                ok=False, order_id=request.order_id, error=str(exc)
            )
        except Exception as exc:
            logger.exception("alpaca order failed")
            self._emit_cancelled(request, f"submit error: {exc}")
            return ExecutionResult(
                ok=False, order_id=request.order_id, error=str(exc)
            )

        broker_order_id = raw.get("id")
        status = raw.get("status")
        self.store.emit(
            OrderSubmittedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=(
                    f"Submitted {request.side} {request.quantity} {request.symbol} "
                    f"to alpaca_paper ({status})"
                ),
                symbol=request.symbol,
                order_id=request.order_id,
                side=request.side,
                quantity=request.quantity,
                price=request.entry_price or 0.0,
                payload={
                    "symbol": request.symbol,
                    "order_id": request.order_id,
                    "side": request.side,
                    "quantity": request.quantity,
                    "price": request.entry_price,
                    "broker_order_id": broker_order_id,
                    "broker_status": status,
                },
            )
        )

        if status == "filled":
            fill_price = float(raw.get("filled_avg_price") or 0) or None
            self.store.emit(
                OrderFilledEvent(
                    timestamp=datetime.now(),
                    mode=self.mode,
                    correlation_id=self.session_id,
                    message=f"Filled {request.symbol} @ {fill_price}",
                    symbol=request.symbol,
                    order_id=request.order_id,
                    fill_price=fill_price or 0.0,
                    fill_quantity=int(float(raw.get("filled_qty") or 0)),
                    payload={
                        "symbol": request.symbol,
                        "order_id": request.order_id,
                        "fill_price": fill_price,
                        "fill_quantity": int(float(raw.get("filled_qty") or 0)),
                    },
                )
            )

        return ExecutionResult(
            ok=True,
            order_id=request.order_id,
            broker_order_id=broker_order_id,
            status=status,
            raw=raw,
        )

    def cancel_entry(
        self, order_id: str, broker_order_id: str | None, symbol: str, reason: str
    ) -> ExecutionResult:
        """Cancel a resting (unfilled) entry order at the broker and record it."""
        try:
            if broker_order_id:
                self.client.cancel_order(broker_order_id)
        except AlpacaApiError as exc:
            # already filled/cancelled at the broker, or transient — log and
            # still emit the cancel so our state stops tracking it
            logger.warning("cancel of %s returned: %s", order_id, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel of %s errored: %s", order_id, exc)
        self._emit_cancelled(
            ExecutionRequest(symbol=symbol, side="buy", quantity=0, order_id=order_id),
            reason,
        )
        return ExecutionResult(ok=True, order_id=order_id, status="cancelled")

    def _emit_cancelled(self, request: ExecutionRequest, reason: str) -> None:
        self.store.emit(
            OrderCancelledEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=f"Order for {request.symbol} cancelled: {reason}",
                symbol=request.symbol,
                order_id=request.order_id,
                cancel_reason=reason,
                payload={
                    "symbol": request.symbol,
                    "order_id": request.order_id,
                    "cancel_reason": reason,
                },
            )
        )
