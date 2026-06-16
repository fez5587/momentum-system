# Data Transformations Flow

## Quick Reference: What Data Flows Where and How It Changes

### INGESTION LAYER
```
Alpaca REST API (live 1-min bars)
  ↓ ingest_live_minute_bars()
  ↓ transform: {symbol, timestamp, open, high, low, close, volume}
  ↓
research/market.duckdb → bars table
  
Alpaca REST API (daily bars history)
  ↓ ingest_daily_history()
  ↓ transform: {symbol, timestamp, open, high, low, close, volume}
  ↓
research/market.duckdb → daily_bars table
```

### DISCOVERY LAYER
```
Manual Symbols (--symbols or WATCHER_SYMBOLS env)
  ├─→ WatchCandidate(symbol, last_price, avg_volume)
  │
Alpaca Most-Actives Screener (if --discover)
  ├─→ WatchCandidate(symbol, last_price, avg_volume)
  │
ResearchWatchlistProvider.get_candidates(session_date)
  ↓ returns: List[WatchCandidate]
  ↓
Watcher ingests as new symbols to watch
```

### EVALUATION LAYER (CORE STRATEGY)
```
research/market.duckdb (minute bars for symbol)
  ↓ query_minute_bars(symbol, session_date)
  ↓ returns: pd.DataFrame with {timestamp, open, high, low, close, volume}
  ↓
evaluate_setup(bars, symbol)
  ├─ Extract metrics:
  │  ├─ Gap% = (today_open - prev_close) / prev_close
  │  ├─ RVOL = current_5min_volume / avg_20day_volume
  │  ├─ Bull flag structure detection
  │  ├─ Price action quality scoring
  │  └─ 5 other criteria (tunable)
  │
  ├─ Evaluate each criterion: pass/fail/not-evaluated
  │
  ├─ Calculate score:
  │  └─ criteria_passed / total_criteria * 100 = X%
  │
  └─ returns: {
       score: float,
       criteria: [
         {name: "gap", passed: true, ...},
         {name: "rvol", passed: false, ...},
         ...
       ],
       entry: float,
       stop: float,
       target: float,
       quality: float
     }
```

### SIGNAL EMISSION
```
evaluate_setup() results
  ↓
  ├─ IF score ≥ ready_score_pct (60%) AND quality ≥ min_quality (30%):
  │  └─ Watcher emits: SignalReadyEvent(
  │       symbol, entry, stop, target, score, criteria, setup_data, ...
  │     )
  │     ✓ Recorded in event store
  │     ✓ Marked as "already signaled" (debounced)
  │
  └─ ELSE:
     └─ Watcher emits: SignalBlockedEvent(
          symbol, reason, score, ...
        )
        ✓ Resets signal debounce
```

### EXECUTION SIZING LAYER
```
SignalReadyEvent(entry, stop, target, ...)
  ↓
TradingExecutionService.tick()
  ├─ Check risk rules:
  │  ├─ open_positions ≥ max_concurrent_positions? → REJECT
  │  ├─ today_realized_pnl ≤ -max_daily_loss_pct * equity? → REJECT
  │  └─ submitted_this_tick ≥ max_orders_per_tick? → SKIP
  │
  └─ calculate_position_size(entry, stop, equity, risk_pct)
     │
     ├─ risk_dollars = equity * risk_pct (e.g., 1% = $1,000 on $100k)
     ├─ risk_per_share = entry - stop
     ├─ shares = risk_dollars / risk_per_share
     └─ returns: {
          shares: int,
          entry: float,
          stop: float,
          target: float (entry + (entry-stop)*reward_multiple),
          risk_dollars: float,
          notional: float (entry * shares)
        }
```

### APPROVAL LAYER
```
ExecutionRequest {shares, entry, stop, target, ...}
  ↓
TradingExecutionService emits: OrderApprovalRequestedEvent
  ├─ Stored in event store
  ├─ Projected into "approval_queue" projection
  └─ Dashboard polls/streams this data
  
Dashboard receives approval request:
  ├─ Shows: symbol, entry, stop, target, shares, risk %, notional
  ├─ Shows: sparkline chart of last 50 bars with entry/stop references
  ├─ User clicks: Approve or Reject
  │
  ├─ ON APPROVE:
  │  └─ Dashboard emits: OrderApprovedEvent(order_id, ...)
  │     ✓ Recorded in event store
  │     ✓ TradingExecutionService detects and submits to broker
  │
  └─ ON REJECT:
     └─ Dashboard emits: OrderRejectedEvent(order_id, reason, ...)
        ✓ Signal removed from queue
        ✓ Session reset for that symbol (can signal again)
```

### BROKER SUBMISSION LAYER
```
OrderApprovedEvent
  ↓
AlpacaPaperExecutor.submit()
  ├─ Create bracket order:
  │  ├─ Entry: limit @ entry_price (can be cancelled/timeout)
  │  ├─ Target: limit @ target_price
  │  └─ Stop: stop @ stop_price (market when hit)
  │
  ├─ Call Alpaca REST API: POST /orders (bracket order)
  │  └─ returns: {id, symbol, qty, status, ...}
  │
  ├─ Emit: OrderSubmittedEvent(broker_order_id, entry, stop, target, ...)
  │  └─ Recorded in event store
  │
  └─ Track in _armed dict:
     └─ _armed[order_id] = {
          symbol, entry_price, broker_order_id,
          armed_at, checks: {timeout, price_break}
        }

Ongoing monitoring:
  ├─ Entry fills?
  │  └─ Position opens (qty shares @ entry_price)
  │
  ├─ Entry timeout? (after entry_timeout_bars minutes)
  │  └─ Cancel unfilled entry order
  │
  ├─ Price breaks back below entry - entry_invalidate_pct?
  │  └─ Cancel unfilled entry order
  │
  └─ Target OR Stop hit?
     └─ Position closes (filled at target or stop)
        ✓ Emit: OrderFilledEvent(symbol, entry, exit, qty, pnl, ...)
        ✓ Recorded in event store
```

### ACCOUNT SYNC LAYER
```
AlpacaPaperSync.sync_account()
  │
  ├─ Alpaca REST GET /account
  │  ↓ transform: {equity, buying_power, long_market_value, ...}
  │  ↓ Emit: AccountSummaryUpdatedEvent(account_snapshot)
  │
  ├─ Alpaca REST GET /positions
  │  ↓ transform: [{symbol, qty, avg_fill, current_price, unrealized_pl}]
  │  ↓ Emit: AccountPositionsUpdatedEvent(positions_list)
  │
  ├─ Alpaca REST GET /orders?status=open
  │  ↓ transform: [{id, symbol, qty, status, filled_qty, ...}]
  │  ↓ Emit: AccountOrdersUpdatedEvent(orders_list)
  │
  └─ Calculate session P&L:
     ├─ Query event_store for all OrderFilledEvent in this session
     ├─ For each: realized_pnl = (exit_price - entry_price) * qty
     ├─ Aggregate: total_realized, total_unrealized, win_rate, avg_r
     └─ Emit: SessionPnlUpdatedEvent(pnl_snapshot)
```

### PROJECTION LAYER (DASHBOARD READ-ONLY VIEWS)
```
EVENT STORE (canonical source)
  ├─ All events append-only, indexed by timestamp + session_id
  │
  └─ Projections (reconstructed views):
     │
     ├─ approval_queue(event_store)
     │  ├─ Filter: OrderApprovalRequestedEvent (status != approved/rejected)
     │  └─ returns: [{order_id, symbol, entry, stop, target, shares, risk%}]
     │
     ├─ account_positions_snapshot(event_store)
     │  ├─ Latest: AccountPositionsUpdatedEvent
     │  └─ returns: [{symbol, qty, entry, current, unrealized_pl}]
     │
     ├─ pnl_snapshot(event_store)
     │  ├─ Latest: SessionPnlUpdatedEvent
     │  └─ returns: {realized, unrealized, total, win_rate, avg_r, open_count, closed_count}
     │
     ├─ activity_feed(event_store, limit=100)
     │  ├─ All events, chronological, most recent first
     │  └─ returns: [{timestamp, event_type, message, symbol, ...}]
     │
     ├─ ready_signals_snapshot(event_store)
     │  ├─ Filter: SignalReadyEvent (not yet approved/rejected)
     │  └─ returns: [{symbol, entry, stop, target, score, criteria}]
     │
     └─ symbol_criteria(event_store, symbol)
        ├─ Latest: CriteriaEvaluatedEvent for this symbol
        └─ returns: [{name, passed, reason, value}] (9 criteria)
```

### DASHBOARD DELIVERY LAYER
```
Projections (from EVENT STORE)
  │
  ├─ SSE Stream (/api/events)
  │  ├─ WebSocket or Server-Sent Events
  │  ├─ Pushes new events in real-time to all connected clients
  │  └─ Format: {event_type, timestamp, symbol, ...}
  │
  └─ REST JSON Endpoints:
     │
     ├─ GET /api/approval-queue
     │  └─ returns: approval_queue projection (JSON)
     │
     ├─ GET /api/positions
     │  └─ returns: account_positions_snapshot projection (JSON)
     │
     ├─ GET /api/pnl
     │  └─ returns: pnl_snapshot projection (JSON)
     │
     ├─ GET /api/activity-feed
     │  └─ returns: activity_feed projection (JSON, last 100 events)
     │
     ├─ GET /api/symbol/:symbol/criteria
     │  └─ returns: 9 criteria with pass/fail/not-evaluated
     │
     ├─ GET /api/symbol/:symbol/bars?minutes=50
     │  └─ returns: [{timestamp, open, high, low, close, volume}]
     │     (for sparkline chart rendering)
     │
     └─ POST /api/approval/:order_id/approve|reject
        └─ Emits OrderApprovedEvent or OrderRejectedEvent
```

### FINAL OUTPUT (DASHBOARD UI)
```
Browser → http://127.0.0.1:8010
  │
  ├─ P&L Strip (top)
  │  └─ Realized | Unrealized | Total | Win Rate | Avg R | Open | Closed
  │     (refreshed by /api/pnl endpoint)
  │
  ├─ Approval Queue (cards)
  │  ├─ Symbol | Entry | Stop | Target | Shares | Risk $ | Risk %
  │  ├─ Sparkline chart (last 50 bars, entry/stop reference lines)
  │  └─ [Approve] [Reject] buttons
  │     (refreshed by /api/approval-queue endpoint or SSE)
  │
  ├─ Open Positions
  │  ├─ Symbol | Qty | Entry | Current | P&L | P&L% | Sparkline
  │  └─ [Exit] button (closes position at market)
  │     (refreshed by /api/positions endpoint)
  │
  ├─ Activity Feed (chronological)
  │  └─ {symbol} {event_type} {timestamp}
  │     (refreshed by /api/activity-feed or SSE)
  │
  └─ Symbol Details (expand on click)
     ├─ Gap: ✓ Pass | 3.2%
     ├─ RVOL: ✗ Fail | 1.2 (need 1.5)
     ├─ Bull Flag: ? Not Evaluated
     └─ ... (9 total criteria)
        (fetched by /api/symbol/:symbol/criteria)
```

---

## Key Data Structure Flows

### Signal Ready → Order Submitted → Position Open → Fill
```
SignalReadyEvent(symbol, entry, stop, target, score, criteria)
  ↓ [user approves in dashboard]
  ↓ OrderApprovedEvent
  ↓ [execution service submits]
  ↓ OrderSubmittedEvent(broker_order_id, entry, stop, target, qty)
  ↓ [Alpaca processes bracket order]
  ↓ [entry order fills]
  ↓ OrderFilledEvent(entry_fill, qty)
  ↓ [position open, waiting for target or stop]
  ↓ [target or stop hit]
  ↓ OrderFilledEvent(exit_fill, qty, pnl)
```

### Symbol State Transitions with Events
```
Symbol Discovered (new symbol added)
  ↓ emit SymbolDiscoveredEvent
  ↓ state = "discovered"
  ↓
Symbol State Changed
  ↓ emit SymbolStateChangedEvent("discovered" → "watching")
  ↓ state = "watching"
  ↓
Criteria Evaluated
  ├─ IF score ≥ threshold:
  │  ├─ emit SignalReadyEvent
  │  ├─ emit SymbolStateChangedEvent("watching" → "ready")
  │  ├─ state = "ready"
  │  └─ signal_debounced = true (no more signal_ready until blocked)
  │
  └─ ELSE:
     ├─ emit SignalBlockedEvent
     ├─ emit SymbolStateChangedEvent("watching" → "blocked")
     ├─ state = "blocked"
     └─ signal_debounced = false (can signal again if recovers)
```

### Position Lifecycle with P&L Calculation
```
Position Opens (OrderFilledEvent: entry)
  ├─ entry_price, qty, entry_time
  ├─ state = "open"
  └─ emit AccountPositionsUpdatedEvent({symbol, qty, entry, current, unrealized_pl})
  
Position Ongoing
  ├─ AccountSummaryUpdatedEvent polls current price
  ├─ unrealized_pl = (current_price - entry_price) * qty
  └─ Dashboard shows live sparkline + unrealized P&L
  
Position Closes (OrderFilledEvent: exit)
  ├─ exit_price, qty, exit_time
  ├─ realized_pnl = (exit_price - entry_price) * qty
  ├─ state = "closed"
  ├─ emit OrderFilledEvent({symbol, entry, exit, qty, pnl, pnl%, exit_reason})
  └─ Dashboard adds to closed trades, updates session P&L
  
Session P&L Aggregation
  ├─ sum all closed_trades.realized_pnl
  ├─ sum all open_positions.unrealized_pl
  ├─ win_count = trades where pnl > 0
  ├─ loss_count = trades where pnl ≤ 0
  ├─ win_rate = win_count / (win_count + loss_count)
  ├─ avg_r = average(pnl / risk_per_trade)
  └─ emit SessionPnlUpdatedEvent({realized, unrealized, total, win_rate, avg_r, counts})
```

---

## Summary: The Loop

Every **tick** (60 seconds):

1. **Ingest** → Market data into research DB
2. **Discover** → Get watchlist symbols
3. **Evaluate** → Run strategy engine on each symbol
4. **Signal** → Emit signal_ready or signal_blocked
5. **Execute** → Check approval queue, submit approved orders
6. **Sync** → Poll Alpaca for account/positions/orders
7. **Project** → Rebuild all read-only views from event store
8. **Deliver** → Push updates to dashboard (SSE or polling)

All transformations are **event-based** and **append-only**. The event store is the single source of truth. The dashboard is a **pure projection** of that store, so any session can be replayed perfectly and audited.
