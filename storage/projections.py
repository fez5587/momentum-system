import json

"""Projections for Milestone 2 read models.

Aggregated views for querying sessions, symbols, approvals, and positions.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def query_session_summary(store) -> list[dict]:
    """Query all session summaries from events."""
    events = store.query_events(event_type="session_summary")

    summaries = []
    for event in events:
        payload = json.loads(event.get("payload_json", "{}"))
        if "session_id" in payload:
            summaries.append(
                {
                    "session_id": payload["session_id"],
                    "event_id": event["id"],
                    "timestamp": event["timestamp"],
                }
            )

    return summaries


def query_symbol_status(store, symbol: str | None = None) -> list[dict]:
    """Query current status for symbols."""
    events = store.query_events(
        event_type="symbol_state_changed",
        symbol=symbol,
    )

    return [
        {
            "event_id": event["id"],
            "symbol": json.loads(event.get("payload_json", "{}")).get("symbol"),
            "previous_state": json.loads(event.get("payload_json", "{}")).get(
                "previous_state"
            ),
            "new_state": json.loads(event.get("payload_json", "{}")).get("new_state"),
            "timestamp": event["timestamp"],
        }
        for event in events
    ]


def query_approval_queue(store) -> list[dict]:
    requested = store.query_events(event_type="order_approval_requested", limit=None)
    approved = store.query_events(event_type="order_approved", limit=None)
    rejected = store.query_events(event_type="order_rejected", limit=None)
    submitted = store.query_events(event_type="order_submitted", limit=None)
    cancelled = store.query_events(event_type="order_cancelled", limit=None)

    resolved_ids = {
        str(json.loads(event.get("payload_json", "{}")).get("order_id"))
        for event in approved + rejected + submitted + cancelled
        if json.loads(event.get("payload_json", "{}")).get("order_id") is not None
    }

    queue = []
    for event in requested:
        payload = json.loads(event.get("payload_json", "{}"))
        order_id = payload.get("order_id")
        if order_id is None or str(order_id) in resolved_ids:
            continue
        queue.append(
            {
                "event_id": event["id"],
                "order_id": str(order_id),
                "symbol": payload.get("symbol"),
                "requested_by": payload.get("requested_by"),
                "approval_mode": payload.get("approval_mode"),
                "execution_mode": payload.get("execution_mode"),
                "execution_request": payload.get("execution_request", {}),
                "timestamp": event["timestamp"],
            }
        )
    return queue


def query_positions_view(store) -> list[dict]:
    """Query all positions (opened/closed events)."""
    opened = store.query_events(event_type="position_opened")
    closed = store.query_events(event_type="position_closed")
    position_events = opened + closed

    positions = []
    for event in position_events:
        payload = json.loads(event.get("payload_json", "{}"))
        positions.append(
            {
                "event_id": event["id"],
                "position_id": payload.get("position_id"),
                "symbol": payload.get("symbol"),
                "event_type": event["event_type"],
                "entry_price": payload.get("entry_price"),
                "quantity": payload.get("quantity"),
                "stop_loss_price": payload.get("stop_loss_price"),
                "exit_price": payload.get("exit_price")
                if event["event_type"] == "position_closed"
                else None,
                "exit_reason": payload.get("exit_reason")
                if event["event_type"] == "position_closed"
                else None,
                "realized_pnl": payload.get("realized_pnl")
                if event["event_type"] == "position_closed"
                else None,
                "timestamp": event["timestamp"],
            }
        )

    return positions


def query_event_timeline(store, session_id: str | None = None) -> list[dict]:
    """Query full event timeline for a session."""
    return store.query_events(session_id=session_id, limit=None)


def query_settings_snapshot(store) -> dict:
    """Query current settings snapshot."""
    events = store.query_events(event_type="settings_snapshot")

    if not events:
        return {}

    latest_event = events[-1]
    return json.loads(latest_event.get("payload_json", "{}"))


def query_ready_signals_snapshot(store, session_id: str | None = None) -> list[dict]:
    """Latest signal_ready per symbol, newest first. Feeds the dashboard."""
    events = store.query_events(
        event_type="signal_ready", session_id=session_id, limit=None
    )
    latest_by_symbol: dict[str, dict] = {}
    for event in events:
        payload = json.loads(event.get("payload_json", "{}"))
        symbol = payload.get("symbol")
        if not symbol:
            continue
        signal_data = payload.get("signal_data") or {}
        latest_by_symbol[str(symbol)] = {
            "symbol": str(symbol),
            "signal_type": payload.get("signal_type"),
            "confidence": payload.get("confidence"),
            "entry_price": signal_data.get("entry_price"),
            "stop_loss_price": signal_data.get("stop_loss_price"),
            "quality_score": signal_data.get("quality_score"),
            "timestamp": event.get("timestamp"),
        }
    return sorted(
        latest_by_symbol.values(),
        key=lambda row: str(row.get("timestamp") or ""),
        reverse=True,
    )


def query_watch_states_snapshot(store, session_id: str | None = None) -> list[dict]:
    state_events = store.query_events(
        event_type="symbol_state_changed",
        session_id=session_id,
        limit=None,
    )
    criteria_events = store.query_events(
        event_type="criteria_evaluated",
        session_id=session_id,
        limit=None,
    )

    latest_by_symbol: dict[str, dict] = {}

    def _blank(symbol_key: str) -> dict:
        return {
            "symbol": symbol_key,
            "state": None,
            "previous_state": None,
            "state_reason": None,
            "last_transition_at": None,
            "last_score": None,
            "last_criteria_at": None,
        }

    for event in criteria_events:
        payload = json.loads(event.get("payload_json", "{}"))
        symbol = payload.get("symbol")
        if not symbol:
            continue
        symbol_key = str(symbol)
        entry = latest_by_symbol.setdefault(symbol_key, _blank(symbol_key))
        entry["last_score"] = payload.get("success_score_pct")
        entry["last_criteria_at"] = event.get("timestamp")

    for event in state_events:
        payload = json.loads(event.get("payload_json", "{}"))
        symbol = payload.get("symbol")
        if not symbol:
            continue
        symbol_key = str(symbol)
        entry = latest_by_symbol.setdefault(symbol_key, _blank(symbol_key))
        entry["state"] = payload.get("new_state")
        entry["previous_state"] = payload.get("previous_state")
        entry["state_reason"] = payload.get("state_reason")
        entry["last_transition_at"] = event.get("timestamp")

    return sorted(latest_by_symbol.values(), key=lambda row: row["symbol"])


def query_account_summary_snapshot(store, broker_name: str | None = None) -> list[dict]:
    events = store.query_events(event_type="account_summary_updated", limit=None)
    latest_by_account: dict[tuple[str, str], dict] = {}

    for event in events:
        payload = json.loads(event.get("payload_json", "{}"))
        broker = str(payload.get("broker_name") or "")
        account_id = str(payload.get("account_id") or "")
        if not broker or not account_id:
            continue
        if broker_name is not None and broker != broker_name:
            continue

        latest_by_account[(broker, account_id)] = {
            "broker_name": broker,
            "account_id": account_id,
            "account_desc": payload.get("account_desc"),
            "total_equity": payload.get("total_equity"),
            "cash_balance": payload.get("cash_balance"),
            "buying_power": payload.get("buying_power"),
            "net_liquidating_value": payload.get("net_liquidating_value"),
            "timestamp": event.get("timestamp"),
        }

    return sorted(
        latest_by_account.values(),
        key=lambda row: (str(row["broker_name"]), str(row["account_id"])),
    )


def query_account_positions_snapshot(
    store, broker_name: str | None = None
) -> list[dict]:
    events = store.query_events(event_type="account_positions_updated", limit=None)
    latest_by_account: dict[tuple[str, str], dict] = {}

    for event in events:
        payload = json.loads(event.get("payload_json", "{}"))
        broker = str(payload.get("broker_name") or "")
        account_id = str(payload.get("account_id") or "")
        if not broker or not account_id:
            continue
        if broker_name is not None and broker != broker_name:
            continue

        latest_by_account[(broker, account_id)] = {
            "broker_name": broker,
            "account_id": account_id,
            "positions": payload.get("positions", []),
            "timestamp": event.get("timestamp"),
        }

    return sorted(
        latest_by_account.values(),
        key=lambda row: (str(row["broker_name"]), str(row["account_id"])),
    )


def query_account_orders_snapshot(store, broker_name: str | None = None) -> list[dict]:
    events = store.query_events(event_type="account_orders_updated", limit=None)
    latest_by_account: dict[tuple[str, str], dict] = {}

    for event in events:
        payload = json.loads(event.get("payload_json", "{}"))
        broker = str(payload.get("broker_name") or "")
        account_id = str(payload.get("account_id") or "")
        if not broker or not account_id:
            continue
        if broker_name is not None and broker != broker_name:
            continue

        latest_by_account[(broker, account_id)] = {
            "broker_name": broker,
            "account_id": account_id,
            "orders": payload.get("orders", []),
            "timestamp": event.get("timestamp"),
        }

    return sorted(
        latest_by_account.values(),
        key=lambda row: (str(row["broker_name"]), str(row["account_id"])),
    )


def query_order_lifecycle_snapshot(store) -> list[dict]:
    event_types = [
        "order_submitted",
        "order_approval_requested",
        "order_approved",
        "order_rejected",
        "order_filled",
        "order_cancelled",
    ]
    latest_by_order: dict[str, dict] = {}

    for event_type in event_types:
        events = store.query_events(event_type=event_type, limit=None)
        for event in events:
            payload = json.loads(event.get("payload_json", "{}"))
            order_id = payload.get("order_id")
            if not order_id:
                continue
            entry = latest_by_order.setdefault(
                str(order_id),
                {
                    "order_id": str(order_id),
                    "symbol": payload.get("symbol"),
                    "status": None,
                    "submitted_at": None,
                    "updated_at": None,
                    "side": None,
                    "quantity": None,
                    "price": None,
                    "fill_price": None,
                    "fill_quantity": None,
                    "approval_mode": None,
                    "rejection_reason": None,
                },
            )

            if event_type == "order_submitted":
                entry["status"] = "submitted"
                entry["submitted_at"] = event.get("timestamp")
                entry["side"] = payload.get("side")
                entry["quantity"] = payload.get("quantity")
                entry["price"] = payload.get("price")
            elif event_type == "order_approval_requested":
                entry["status"] = "approval_requested"
                entry["approval_mode"] = payload.get("approval_mode")
            elif event_type == "order_approved":
                entry["status"] = "approved"
            elif event_type == "order_rejected":
                entry["status"] = "rejected"
                entry["rejection_reason"] = payload.get("rejection_reason")
            elif event_type == "order_filled":
                entry["status"] = "filled"
                entry["fill_price"] = payload.get("fill_price")
                entry["fill_quantity"] = payload.get("fill_quantity")
            elif event_type == "order_cancelled":
                entry["status"] = "cancelled"
                entry["rejection_reason"] = payload.get("cancel_reason")

            entry["updated_at"] = event.get("timestamp")

    return sorted(latest_by_order.values(), key=lambda row: row["order_id"])


# ---------------------------------------------------------------------------
# Interactive dashboard projections (criteria detail, session P&L, fills feed)
# ---------------------------------------------------------------------------

# Human-readable labels for the nine setup criteria, in evaluation order.
CRITERIA_LABELS = {
    "sufficient_data": "Sufficient data",
    "gap": "Gap %",
    "relative_volume": "Relative volume",
    "impulse": "Impulse leg",
    "pullback": "Pullback depth",
    "pullback_volume": "Pullback volume",
    "vwap": "Holding VWAP",
    "candle_quality": "Candle quality",
    "breakout": "Breakout trigger",
}


def query_symbol_criteria(store, symbol: str, session_id: str | None = None) -> dict:
    """Latest per-criterion pass/fail breakdown for one symbol.

    Powers the click-to-expand criteria panel: returns every criterion with a
    passed flag and label so a 'blocked' symbol becomes legible at a glance.
    """
    events = store.query_events(
        event_type="criteria_evaluated",
        symbol=symbol,
        session_id=session_id,
        limit=None,
    )
    if not events:
        return {"symbol": symbol, "criteria": [], "score": None, "evaluated_at": None}

    latest = events[-1]
    payload = json.loads(latest.get("payload_json", "{}"))
    results = payload.get("criteria_results") or {}
    passed = set(results.get("passed") or [])
    failed = set(results.get("failed") or [])

    criteria = []
    for key, label in CRITERIA_LABELS.items():
        if key in passed:
            state = True
        elif key in failed:
            state = False
        else:
            state = None  # not evaluated this pass
        criteria.append({"key": key, "label": label, "passed": state})

    return {
        "symbol": symbol,
        "criteria": criteria,
        "passed_count": payload.get("passed_criteria"),
        "total_count": payload.get("total_criteria"),
        "score": payload.get("success_score_pct"),
        "status": payload.get("status"),
        "evaluated_at": latest.get("timestamp"),
    }


def query_fills_feed(store, limit: int = 50) -> list[dict]:
    """Chronological fills + submissions for the activity feed (newest first)."""
    rows: list[dict] = []
    for event_type in ("order_filled", "order_submitted"):
        for event in store.query_events(event_type=event_type, limit=None):
            payload = json.loads(event.get("payload_json", "{}"))
            rows.append(
                {
                    "kind": event_type,
                    "order_id": payload.get("order_id"),
                    "symbol": payload.get("symbol"),
                    "side": payload.get("side"),
                    "quantity": payload.get("fill_quantity") or payload.get("quantity"),
                    "price": payload.get("fill_price") or payload.get("price"),
                    "timestamp": event.get("timestamp"),
                }
            )
    rows.sort(key=lambda r: str(r.get("timestamp") or ""), reverse=True)
    return rows[:limit]


def _r_multiple(entry, stop, exit_price, side: str) -> float | None:
    """Realized R-multiple given entry/stop/exit. None if stop distance is 0."""
    try:
        entry = float(entry)
        stop = float(stop)
        exit_price = float(exit_price)
    except (TypeError, ValueError):
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    direction = 1.0 if side != "sell" else -1.0
    return round((exit_price - entry) * direction / risk, 2)


def query_session_pnl(store, session_id: str | None = None) -> dict:
    """Day P&L + trade stats from position_closed events and live positions.

    Realized stats come from closed positions; unrealized comes from the most
    recent account positions snapshot so the strip matches the broker.
    """
    closed = store.query_events(event_type="position_closed", limit=None)

    realized = 0.0
    wins = 0
    losses = 0
    r_multiples: list[float] = []
    trades: list[dict] = []

    for event in closed:
        payload = json.loads(event.get("payload_json", "{}"))
        pnl = payload.get("realized_pnl")
        if pnl is None:
            continue
        pnl = float(pnl)
        realized += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        r = _r_multiple(
            payload.get("entry_price"),
            payload.get("stop_loss_price"),
            payload.get("exit_price"),
            payload.get("side") or "buy",
        )
        if r is not None:
            r_multiples.append(r)
        trades.append(
            {
                "symbol": payload.get("symbol"),
                "realized_pnl": round(pnl, 2),
                "r_multiple": r,
                "exit_reason": payload.get("exit_reason"),
                "timestamp": event.get("timestamp"),
            }
        )

    # unrealized from the latest positions snapshot (any broker)
    unrealized = 0.0
    open_positions = 0
    snapshots = query_account_positions_snapshot(store)
    if snapshots:
        for position in snapshots[-1].get("positions") or []:
            open_positions += 1
            try:
                unrealized += float(position.get("unrealized_pl") or 0.0)
            except (TypeError, ValueError):
                pass

    closed_count = wins + losses
    win_rate = round(wins / closed_count, 3) if closed_count else None
    avg_r = round(sum(r_multiples) / len(r_multiples), 2) if r_multiples else None

    # BROKER-AUTHORITATIVE day P&L. The closed-event `realized` above is $0
    # whenever position_closed events aren't emitted (they currently aren't), so
    # the strip read $0 on a real -$2,322 day — the operator flew blind. Prefer
    # the broker equity delta: day P&L = latest equity - prior-session close
    # (last_equity); matched (realized) = day P&L - current unrealized.
    broker_day = None
    summ = store.query_events(event_type="account_summary_updated", limit=None)
    if summ:
        sp = json.loads(summ[-1].get("payload_json", "{}"))  # ASC order -> latest
        try:
            eq = float(sp.get("total_equity") or 0.0)
            le = float(sp.get("last_equity") or 0.0)
            if eq and le:
                broker_day = eq - le
        except (TypeError, ValueError):
            pass

    if broker_day is not None:
        realized_out = round(broker_day - unrealized, 2)   # broker-authoritative matched
        total_out = round(broker_day, 2)
        pnl_source = "broker"
    else:
        realized_out = round(realized, 2)
        total_out = round(realized + unrealized, 2)
        pnl_source = "closed_events"

    return {
        "realized_pnl": realized_out,
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": total_out,
        "broker_day_pnl": round(broker_day, 2) if broker_day is not None else None,
        "pnl_source": pnl_source,
        "wins": wins,
        "losses": losses,
        "closed_trades": closed_count,
        "open_positions": open_positions,
        "win_rate": win_rate,
        "avg_r_multiple": avg_r,
        "expectancy_r": avg_r,
        "trades": list(reversed(trades))[:20],
    }
