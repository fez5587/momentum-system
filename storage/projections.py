import json

"""Projections for Milestone 2 read models.

Aggregated views for querying sessions, symbols, approvals, and positions.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def _evt_window(for_date: str | None = None) -> tuple[str | None, str | None]:
    """(since, until) event-time bounds for a dashboard view.

    LIVE (None / 'live' / 'today'): the last 45 min — enough to surface the
    LATEST snapshot of every account/symbol while turning a full-history scan
    into a tiny range read (the dashboard projections only need the latest, but
    used to load the ENTIRE history — 112s on watch_states, 78s on orders).

    A 'YYYY-MM-DD': that whole local calendar day, so the date picker can review
    a past session's end-of-day P&L / trades / activity.
    """
    from datetime import datetime, timedelta
    if for_date and str(for_date).lower() not in ("live", "today", ""):
        try:
            d = datetime.fromisoformat(str(for_date)).date()
            start = datetime(d.year, d.month, d.day)
            return (start.isoformat(), (start + timedelta(days=1)).isoformat())
        except (TypeError, ValueError):
            pass
    return ((datetime.now() - timedelta(minutes=45)).isoformat(), None)


def _state_until(for_date: str | None = None) -> str | None:
    """Upper time bound for a 'latest state' read: end-of-day for a historical
    date, None (= now) for live."""
    from datetime import datetime, timedelta
    if for_date and str(for_date).lower() not in ("live", "today", ""):
        try:
            d = datetime.fromisoformat(str(for_date)).date()
            return (datetime(d.year, d.month, d.day) + timedelta(days=1)).isoformat()
        except (TypeError, ValueError):
            pass
    return None


def _hist_window(for_date: str | None = None) -> tuple[str | None, str | None]:
    """(since, until) for DAY-SCOPED reads (fills, closed trades). Live = UNBOUNDED
    (these event types are tiny — fills/closed are rare), so an idle loop or a
    test's fixed-date fixtures still show. A 'YYYY-MM-DD' = that whole day."""
    from datetime import datetime, timedelta
    if for_date and str(for_date).lower() not in ("live", "today", ""):
        try:
            d = datetime.fromisoformat(str(for_date)).date()
            start = datetime(d.year, d.month, d.day)
            return (start.isoformat(), (start + timedelta(days=1)).isoformat())
        except (TypeError, ValueError):
            pass
    return (None, None)


def _latest_event_payload(store, event_type: str, until: str | None = None):
    """The single most-recent event payload of a type (optionally before
    `until`) via a DESC-LIMIT-1 index seek — O(1) vs loading the whole history.
    Used by the latest-state snapshots (account orders/positions carry big
    payloads; loading every one just to take the newest was 78s)."""
    safe_type = "".join(c for c in str(event_type) if c.isalnum() or c == "_")
    cond = f"event_type = '{safe_type}'"
    if until:
        safe_until = str(until).replace("'", "")
        cond += f" AND timestamp < '{safe_until}'"
    try:
        rows = store.con.execute(
            f"SELECT payload_json, timestamp FROM events WHERE {cond} "
            f"ORDER BY timestamp DESC LIMIT 1"
        ).fetchall()
        return rows[0] if rows else None
    except Exception:  # noqa: BLE001
        return None


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


def query_watch_states_snapshot(store, session_id: str | None = None,
                                for_date: str | None = None) -> list[dict]:
    since, until = _evt_window(for_date)
    state_events = store.query_events(
        event_type="symbol_state_changed",
        session_id=session_id, since=since, until=until,
        limit=None,
    )
    criteria_events = store.query_events(
        event_type="criteria_evaluated",
        session_id=session_id, since=since, until=until,
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


def query_account_summary_snapshot(store, broker_name: str | None = None,
                                   for_date: str | None = None) -> list[dict]:
    row = _latest_event_payload(store, "account_summary_updated", _state_until(for_date))
    if not row:
        return []
    payload = json.loads(row[0] or "{}")
    broker = str(payload.get("broker_name") or "")
    account_id = str(payload.get("account_id") or "")
    if not broker or not account_id or (broker_name is not None and broker != broker_name):
        return []
    return [{
        "broker_name": broker, "account_id": account_id,
        "account_desc": payload.get("account_desc"),
        "total_equity": payload.get("total_equity"),
        "cash_balance": payload.get("cash_balance"),
        "buying_power": payload.get("buying_power"),
        "net_liquidating_value": payload.get("net_liquidating_value"),
        "timestamp": row[1],
    }]


def query_account_positions_snapshot(
    store, broker_name: str | None = None, for_date: str | None = None
) -> list[dict]:
    row = _latest_event_payload(store, "account_positions_updated", _state_until(for_date))
    if not row:
        return []
    payload = json.loads(row[0] or "{}")
    broker = str(payload.get("broker_name") or "")
    account_id = str(payload.get("account_id") or "")
    if not broker or not account_id or (broker_name is not None and broker != broker_name):
        return []
    return [{"broker_name": broker, "account_id": account_id,
             "positions": payload.get("positions", []), "timestamp": row[1]}]


def query_account_orders_snapshot(store, broker_name: str | None = None,
                                  for_date: str | None = None) -> list[dict]:
    # O(1) latest event — these payloads are huge (every order incl. legs), so
    # loading the full history to take the newest was the 78s offender.
    row = _latest_event_payload(store, "account_orders_updated", _state_until(for_date))
    if not row:
        return []
    payload = json.loads(row[0] or "{}")
    broker = str(payload.get("broker_name") or "")
    account_id = str(payload.get("account_id") or "")
    if not broker or not account_id or (broker_name is not None and broker != broker_name):
        return []
    return [{"broker_name": broker, "account_id": account_id,
             "orders": payload.get("orders", []), "timestamp": row[1]}]


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


def query_fills_feed(store, limit: int = 50, for_date: str | None = None) -> list[dict]:
    """Chronological fills + submissions for the activity feed (newest first)."""
    since, until = _hist_window(for_date)
    rows: list[dict] = []
    for event_type in ("order_filled", "order_submitted"):
        for event in store.query_events(event_type=event_type, since=since,
                                        until=until, limit=None):
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


def query_session_pnl(store, session_id: str | None = None,
                      for_date: str | None = None) -> dict:
    """Day P&L + trade stats. Realized comes from the broker equity delta (the
    closed-event reconstruction reads $0); unrealized from the latest positions
    snapshot. ``for_date`` scopes it to a past session for the date picker."""
    since, until = _hist_window(for_date)
    closed = store.query_events(event_type="position_closed",
                                since=since, until=until, limit=None)

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
    snapshots = query_account_positions_snapshot(store, for_date=for_date)
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
    _ar = _latest_event_payload(store, "account_summary_updated", _state_until(for_date))
    if _ar:
        sp = json.loads(_ar[0] or "{}")  # latest equity snapshot (O(1))
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


_PROT_ACTIVE = {"held", "new", "accepted", "pending_new",
                "accepted_for_bidding", "partially_filled"}


def query_risk_state(store, for_date: str | None = None) -> dict:
    """Portfolio risk/health for the dashboard gauge — the conditions that
    silently cost money this session: concentration vs the caps, day P&L vs the
    daily-loss limit, data freshness, and any UNPROTECTED (naked) long. Returns a
    status (ok / warn / critical) plus the underlying numbers."""
    import os
    from datetime import datetime, timezone

    equity = None
    day_pnl = None
    _ar = _latest_event_payload(store, "account_summary_updated", _state_until(for_date))
    ts_latest = None
    if _ar:
        sp = json.loads(_ar[0] or "{}")
        ts_latest = _ar[1]
        try:
            equity = float(sp.get("total_equity") or 0.0) or None
            le = float(sp.get("last_equity") or 0.0)
            if equity and le:
                day_pnl = equity - le
        except (TypeError, ValueError):
            pass

    psnap = query_account_positions_snapshot(store, for_date=for_date)
    positions = (psnap[-1].get("positions") if psnap else []) or []
    osnap = query_account_orders_snapshot(store, for_date=for_date)
    orders = (osnap[-1].get("orders") if osnap else []) or []

    sell_cover: dict = {}
    for o in orders:
        if (o.get("side") == "sell" and o.get("status") in _PROT_ACTIVE
                and o.get("type") in ("stop", "stop_limit", "limit", "trailing_stop")):
            q = float(o.get("quantity") or 0) - float(o.get("filled_quantity") or 0)
            sell_cover[o.get("symbol")] = sell_cover.get(o.get("symbol"), 0.0) + max(q, 0.0)

    open_count = 0
    gross = 0.0
    unprotected: list[str] = []
    for p in positions:
        try:
            qty = float(p.get("quantity") if p.get("quantity") is not None
                        else p.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue
        open_count += 1
        try:
            gross += abs(float(p.get("market_value") or 0.0))
        except (TypeError, ValueError):
            pass
        if sell_cover.get(p.get("symbol"), 0.0) < qty - 1e-6:
            unprotected.append(p.get("symbol"))

    gross_pct = (gross / equity) if equity else None
    day_pnl_pct = (day_pnl / equity) if (equity and day_pnl is not None) else None

    age = None  # data freshness from the latest broker-sync timestamp
    if ts_latest:
        try:
            t = (datetime.fromisoformat(ts_latest.replace("Z", "+00:00"))
                 if isinstance(ts_latest, str) else ts_latest)
            if t.tzinfo is None:
                # event timestamps are stored NAIVE LOCAL (datetime.now()), so
                # compare against naive-local now — comparing to UTC would be off
                # by the whole timezone offset (the chip read ~4h on fresh data).
                age = (datetime.now() - t).total_seconds()
            else:
                age = (datetime.now(timezone.utc) - t).total_seconds()
        except Exception:  # noqa: BLE001
            pass

    max_conc = int(os.environ.get("TRADING_MAX_CONCURRENT_POSITIONS", "6"))
    max_gross = float(os.environ.get("TRADING_MAX_GROSS_NOTIONAL_PCT", "0.60"))
    daily_loss = float(os.environ.get("TRADING_MAX_DAILY_LOSS_PCT", "0.03"))

    status = "ok"
    if unprotected:
        status = "critical"
    elif day_pnl_pct is not None and day_pnl_pct <= -daily_loss * 0.8:
        status = "critical"
    elif (open_count >= max_conc
          or (gross_pct is not None and gross_pct >= max_gross * 0.9)
          or (day_pnl_pct is not None and day_pnl_pct <= -daily_loss * 0.5)
          or (age is not None and age > 90)):
        status = "warn"

    return {
        "status": status,
        "open_positions": open_count,
        "max_concurrent": max_conc,
        "gross_pct": round(gross_pct, 3) if gross_pct is not None else None,
        "max_gross_pct": max_gross,
        "day_pnl": round(day_pnl, 2) if day_pnl is not None else None,
        "day_pnl_pct": round(day_pnl_pct, 4) if day_pnl_pct is not None else None,
        "daily_loss_limit_pct": daily_loss,
        "data_age_s": round(age, 1) if age is not None else None,
        "unprotected": unprotected,
    }


def query_armed_triggers(store) -> dict:
    """The live armed-trigger book — the heart of the ORB strategy: which gappers
    are loaded and how close each is to firing its opening-range breakout. The
    fast trigger thread emits this every few seconds as a module_tick(triggers);
    the dashboard renders it as a proximity radar. Bounded to the last 15 min so
    it's a cheap range scan, not a full module_tick load."""
    from datetime import datetime, timedelta
    since = (datetime.now() - timedelta(minutes=15)).isoformat()
    events = store.query_events(event_type="module_tick", since=since, limit=None)
    for event in reversed(events):  # ASC order -> newest first
        p = json.loads(event.get("payload_json", "{}"))
        if p.get("module") == "triggers":
            m = p.get("metrics") or {}
            return {"triggers": m.get("triggers") or [],
                    "armed": m.get("armed", 0),
                    "timestamp": event.get("timestamp")}
    return {"triggers": [], "armed": 0, "timestamp": None}


def query_equity_curve(store, points: int = 200, for_date: str | None = None) -> dict:
    """Total-equity series for an intraday P&L-shape sparkline, plus the prior-
    session-close baseline (last_equity). Live = last 10h (the trading day); a
    YYYY-MM-DD = that whole day, for the date picker."""
    from datetime import datetime, timedelta
    if for_date and str(for_date).lower() not in ("live", "today", ""):
        since, until = _evt_window(for_date)
    else:
        since, until = (datetime.now() - timedelta(hours=10)).isoformat(), None
    events = store.query_events(event_type="account_summary_updated",
                                since=since, until=until, limit=None)
    series: list[float] = []
    baseline = None
    for e in events:
        p = json.loads(e.get("payload_json", "{}"))
        try:
            eq = float(p.get("total_equity") or 0.0)
            if eq:
                series.append(round(eq, 2))
            if baseline is None:
                le = float(p.get("last_equity") or 0.0)
                if le:
                    baseline = round(le, 2)
        except (TypeError, ValueError):
            pass
    if len(series) > points:  # keep the most recent N
        series = series[-points:]
    return {"equity": series, "baseline": baseline if baseline else (series[0] if series else None)}
