"""Sync the Alpaca paper account into the event store.

Emits account_summary_updated / account_positions_updated /
account_orders_updated events that the dashboard projections read.
"""

from __future__ import annotations

import logging
from datetime import datetime

import json

from alpaca_paper.client import AlpacaPaperClient
from storage.event_schema import (
    AccountOrdersUpdatedEvent,
    AccountPositionsUpdatedEvent,
    AccountSummaryUpdatedEvent,
    BrokerHealthChangedEvent,
    EventMode,
    PositionClosedEvent,
)
from storage.event_store import EventStore

# Alpaca order type -> trade-journal exit reason
_EXIT_REASON = {
    "stop": "stop_loss", "stop_limit": "stop_loss", "trailing_stop": "trailing_stop",
    "limit": "take_profit", "market": "market_exit",
}

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
        # last seen open positions, to detect closes by diffing snapshots. None
        # until the first sync, when it's seeded from the last PERSISTED snapshot
        # so a close that happened while the loop was down is still journaled.
        self._prev_positions: list[dict] | None = None

    def _emit_health(self, reason: str) -> None:
        """Make a broker sync failure VISIBLE (else stale snapshots look current)."""
        self.store.emit(
            BrokerHealthChangedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=f"alpaca_paper sync degraded: {reason}",
                broker_name=BROKER_NAME,
                previous_health="healthy",
                new_health="degraded",
                health_reason=reason,
            )
        )

    def sync_account(self) -> dict | None:
        try:
            account = self.client.get_account()
        except Exception as exc:
            logger.warning("alpaca account sync failed: %s", exc)
            self._emit_health(f"account: {exc}")
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
                last_equity=float(account.get("last_equity") or 0),
            )
        )
        return account

    def sync_positions(self) -> list[dict] | None:
        try:
            raw = self.client.get_positions()
        except Exception as exc:
            logger.warning("alpaca positions sync failed: %s", exc)
            self._emit_health(f"positions: {exc}")
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

    def sync_orders(self, status: str = "all", limit: int = 500) -> list[dict] | None:
        try:
            raw = self.client.get_orders(status=status, limit=limit, nested=True)
        except Exception as exc:
            logger.warning("alpaca orders sync failed: %s", exc)
            self._emit_health(f"orders: {exc}")
            return None

        def _flat(o: dict, parent: dict | None = None) -> dict:
            return {
                "broker_order_id": o.get("id"),
                "client_order_id": o.get("client_order_id"),
                # bracket child legs (stop/take-profit) don't repeat the symbol —
                # inherit it from the parent so an exit fill isn't dropped.
                "symbol": o.get("symbol") or (parent or {}).get("symbol"),
                "side": o.get("side"),
                "quantity": float(o.get("qty") or 0),
                "filled_quantity": float(o.get("filled_qty") or 0),
                "type": o.get("type"),
                "status": o.get("status"),
                "limit_price": o.get("limit_price"),
                # capture the stop trigger too — needed for the dashboard R bar /
                # spark stop-line / any stop-level read (only limit_price was kept).
                "stop_price": o.get("stop_price"),
                "filled_avg_price": o.get("filled_avg_price"),
                "submitted_at": o.get("submitted_at"),
            }

        # Flatten parent orders AND their child legs. A position exited by its
        # bracket STOP or TAKE-PROFIT leg has that exit fill as a LEG, not a
        # top-level order — recording only parents made every stop/TP exit
        # invisible (positions showed falsely "open", realized P&L understated).
        orders = []
        for o in raw:
            orders.append(_flat(o))
            for leg in (o.get("legs") or []):
                orders.append(_flat(leg, parent=o))
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

    def _load_last_persisted_positions(self) -> list[dict]:
        """The most recent persisted positions snapshot (prior session/run), so
        the first reconcile after a restart can still catch a close that happened
        while we were down. [] if none / on any fault."""
        try:
            rows = self.store.con.execute(
                "SELECT payload_json FROM events "
                "WHERE event_type = 'account_positions_updated' "
                "ORDER BY timestamp DESC, created_at DESC LIMIT 1"
            ).fetchall()
            if rows:
                return json.loads(rows[0][0] or "{}").get("positions") or []
        except Exception:  # noqa: BLE001
            pass
        return []

    def _find_exit(self, symbol: str, is_short: bool, orders: list[dict]):
        """(exit_price, exit_reason, stop_level, exit_order_id) for a now-closed
        symbol, read from the filled exit leg in the orders snapshot. The exit of
        a long is a filled SELL (a short, a filled BUY). Also surfaces the bracket
        stop level (filled or not) for the R-multiple. Any None -> caller falls
        back to the last mark."""
        exit_side = "buy" if is_short else "sell"
        stop_level = None
        fills = []
        for o in orders:
            if o.get("symbol") != symbol:
                continue
            otype = str(o.get("type") or "")
            if otype in ("stop", "stop_limit") and o.get("stop_price") not in (None, ""):
                try:
                    stop_level = float(o.get("stop_price"))
                except (TypeError, ValueError):
                    pass
            if (str(o.get("side") or "").lower() == exit_side
                    and str(o.get("status") or "").lower() == "filled"
                    and o.get("filled_avg_price") not in (None, "")):
                fills.append(o)
        if not fills:
            return None, None, stop_level, None
        fills.sort(key=lambda o: str(o.get("submitted_at") or ""), reverse=True)
        f = fills[0]
        try:
            px = float(f.get("filled_avg_price"))
        except (TypeError, ValueError):
            px = None
        reason = _EXIT_REASON.get(str(f.get("type") or ""), "closed")
        return px, reason, stop_level, f.get("broker_order_id")

    def reconcile_closed(self, prev_positions, new_positions, orders) -> int:
        """Emit a position_closed event for every symbol that was open in the
        previous snapshot and is gone (or flat) in the new one. This is the ONLY
        place the trade journal learns about exits — bracket stops/targets fill
        broker-side and EOD flatten closes via market orders, so a snapshot diff
        is the one signal that catches them all. Realized $ stays broker-
        authoritative in the P&L strip; these events feed wins/losses/avg-R/the
        trade list. Returns the number emitted."""
        new_open = {p.get("symbol") for p in (new_positions or [])
                    if abs(float(p.get("quantity") or 0)) > 0}
        emitted = 0
        for prev in prev_positions or []:
            sym = prev.get("symbol")
            qty = abs(float(prev.get("quantity") or 0))
            if not sym or qty <= 0 or sym in new_open:
                continue
            entry = float(prev.get("avg_entry_price") or 0)
            is_short = str(prev.get("side") or "long").lower() == "short"
            exit_price, reason, stop_level, oid = self._find_exit(sym, is_short, orders or [])
            if exit_price is None:                       # no fill seen yet -> last mark
                cp = prev.get("current_price")
                exit_price = float(cp) if cp not in (None, "") else entry
            reason = reason or "closed"
            realized = (entry - exit_price) * qty if is_short else (exit_price - entry) * qty
            ts = datetime.now()
            self.store.emit(PositionClosedEvent(
                timestamp=ts, mode=self.mode, correlation_id=self.session_id,
                message=f"{sym} closed @ {exit_price:.4f} ({reason}) pnl={realized:+.2f}",
                position_id=str(oid or f"{sym}-{ts.isoformat()}"),
                symbol=sym, exit_price=round(exit_price, 4), exit_reason=reason,
                realized_pnl=round(realized, 2), entry_price=round(entry, 4),
                stop_loss_price=round(stop_level, 4) if stop_level else None,
                side=("sell" if is_short else "buy"), quantity=qty,
            ))
            emitted += 1
        return emitted

    def sync_all(self) -> dict:
        # seed the close-detection baseline from the last persisted snapshot
        # BEFORE this cycle emits a fresh one (else prev == current => no diff).
        if self._prev_positions is None:
            self._prev_positions = self._load_last_persisted_positions()
        account = self.sync_account()
        positions = self.sync_positions()
        orders = self.sync_orders()
        # only reconcile on a good positions read; keep the last baseline otherwise
        if positions is not None:
            try:
                self.reconcile_closed(self._prev_positions, positions, orders or [])
            except Exception as exc:  # noqa: BLE001 — never let journaling break sync
                logger.warning("position-close reconcile failed: %s", exc)
            self._prev_positions = positions
        return {"account": account, "positions": positions, "orders": orders}
