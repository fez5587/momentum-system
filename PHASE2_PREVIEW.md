# Phase 2 Preview: What You'll Be Coding

This preview shows **exactly** what Phase 2 implementation looks like, so you can see the work upfront.

---

## Example 1: Instrumenting `ingest_live_minute_bars()`

### Before (Phase 1)
```python
def ingest_live_minute_bars(alpaca_client, research_db, symbols):
    """Ingest latest 1-minute bars from Alpaca."""
    for symbol in symbols:
        bars = alpaca_client.get_bars(symbol, lookback_minutes=120)
        # Insert into DB silently...
        research_db.insert_bars(symbol, bars)
    # No telemetry, no validation, silent success/failure
```

### After (Phase 2)
```python
from core.telemetry import TelemetryCollector, emit_module_tick, emit_data_validation
from core.validation import BarDataValidator

def ingest_live_minute_bars(alpaca_client, research_db, symbols):
    """Ingest latest 1-minute bars from Alpaca with full telemetry."""
    
    collector = TelemetryCollector("ingestion")
    validation_failures = []
    
    try:
        for symbol in symbols:
            try:
                bars = alpaca_client.get_bars(symbol, lookback_minutes=120)
                collector.record_input(len(bars))
                
                # Validate each bar before insertion
                for i, bar in enumerate(bars):
                    is_valid, failures = BarDataValidator.validate_bar(bar)
                    if not is_valid:
                        collector.record_error()
                        validation_failures.extend(failures)
                
                # Insert valid bars
                if not validation_failures:
                    research_db.insert_bars(symbol, bars)
                    collector.record_output(len(bars))
                    collector.record_metric(f"{symbol}_volume_avg", 
                                          sum(b['volume'] for b in bars) / len(bars))
                        
            except Exception as e:
                collector.record_error()
                collector.record_warning(f"{symbol} failed: {e}")
        
        # Emit validation checkpoint
        emit_data_validation(
            module_name="ingestion",
            validation_type="schema",
            valid=len([f for f in validation_failures if f['severity'] == 'error']) == 0,
            details={
                "rows_validated": collector.input_count,
                "validation_failures": validation_failures,
                "symbols_processed": len(symbols)
            }
        )
        
        # Emit completion telemetry
        metrics = collector.finalize()
        emit_module_tick(
            module_name="ingestion",
            stage="completed",
            metrics=metrics
        )
        
    except Exception as e:
        collector.record_error()
        metrics = collector.finalize()
        emit_module_tick(
            module_name="ingestion",
            stage="failed",
            metrics=metrics,
            errors=[{"message": str(e)}]
        )
        raise
```

**What changed:**
- ✅ Wrapped with `TelemetryCollector`
- ✅ Validate bars before insertion
- ✅ Record input/output counts
- ✅ Track custom metrics (volume averages)
- ✅ Emit validation + completion events
- ✅ Error handling with telemetry

**You'll see in event store:**
```json
{
  "type": "module_tick",
  "module": "ingestion",
  "stage": "completed",
  "duration_ms": 245,
  "input_count": 500,
  "output_count": 500,
  "metrics": {
    "AAPL_volume_avg": 125000,
    "TSLA_volume_avg": 98000,
    "AMD_volume_avg": 75000
  },
  "errors": []
}
```

---

## Example 2: Instrumenting `evaluate_setup()`

### Before (Phase 1)
```python
def evaluate_setup(bars, symbol, previous_close, avg_daily_volume):
    """Evaluate setup criteria."""
    
    gap_pct = ((bars[-1]['open'] - previous_close) / previous_close) * 100
    rvol = bars[-1]['volume'] / avg_daily_volume
    
    # ... evaluate 7 more criteria silently ...
    
    criteria_passed = sum([gap_pct >= 2.0, rvol >= 1.5, ...])
    score = (criteria_passed / 9) * 100
    
    return {
        'status': 'ready' if score >= 60 else 'blocked',
        'score': score,
        # ... hidden metrics ...
    }
    # User never sees WHY it passed/failed
```

### After (Phase 2)
```python
from core.telemetry import (
    TelemetryCollector, 
    emit_module_tick, 
    emit_symbol_evaluation_detail
)
from core.validation import RangeValidator

def evaluate_setup(bars, symbol, previous_close, avg_daily_volume):
    """Evaluate setup criteria with full telemetry."""
    
    collector = TelemetryCollector("evaluation")
    criteria_results = []
    
    try:
        collector.record_input(1)  # 1 symbol evaluated
        
        # STAGE 1: Gap check
        gap_pct = ((bars[-1]['open'] - previous_close) / previous_close) * 100
        gap_passed = gap_pct >= 2.0
        
        emit_symbol_evaluation_detail(
            symbol=symbol,
            stage="gap_checked",
            metrics={"gap_pct": gap_pct, "threshold": 2.0},
            passed=gap_passed,
            blocking_reason=None if gap_passed else f"Gap {gap_pct:.1f}% < 2.0%"
        )
        collector.record_metric("gap_pct", gap_pct)
        criteria_results.append(("gap", gap_passed, gap_pct))
        
        # STAGE 2: RVOL check
        current_5min_vol = bars[-1]['volume']
        rvol = current_5min_vol / avg_daily_volume
        rvol_passed = rvol >= 1.5
        
        emit_symbol_evaluation_detail(
            symbol=symbol,
            stage="rvol_calculated",
            metrics={
                "rvol": rvol,
                "threshold": 1.5,
                "current_5min_vol": current_5min_vol,
                "avg_20day_vol": avg_daily_volume
            },
            passed=rvol_passed,
            blocking_reason=None if rvol_passed else f"RVOL {rvol:.2f}x < 1.5x"
        )
        collector.record_metric("rvol", rvol)
        criteria_results.append(("rvol", rvol_passed, rvol))
        
        # STAGE 3: Bull flag detection
        bull_flag_score, bull_flag_details = detect_bull_flag(bars)
        bull_flag_passed = bull_flag_score >= 0.5
        
        emit_symbol_evaluation_detail(
            symbol=symbol,
            stage="bull_flag_detected",
            metrics={
                "confidence": bull_flag_score,
                "threshold": 0.5,
                "details": bull_flag_details
            },
            passed=bull_flag_passed,
            blocking_reason=None if bull_flag_passed else f"Bull flag confidence {bull_flag_score:.2f} < 0.5"
        )
        collector.record_metric("bull_flag_confidence", bull_flag_score)
        criteria_results.append(("bull_flag", bull_flag_passed, bull_flag_score))
        
        # ... evaluate 6 more criteria, each with emit_symbol_evaluation_detail ...
        
        # Calculate final score
        criteria_passed = sum(1 for _, passed, _ in criteria_results if passed)
        total_criteria = len(criteria_results)
        score = (criteria_passed / total_criteria) * 100
        quality = bull_flag_score * (rvol / 1.5)  # Custom quality metric
        
        emit_symbol_evaluation_detail(
            symbol=symbol,
            stage="score_calculated",
            metrics={
                "score": score,
                "criteria_passed": criteria_passed,
                "total_criteria": total_criteria,
                "quality": quality,
                "threshold": 60
            },
            passed=score >= 60,
            blocking_reason=None if score >= 60 else f"Score {score:.1f}% < 60%"
        )
        collector.record_metric("score", score)
        collector.record_metric("quality", quality)
        
        # Determine status
        status = "ready" if score >= 60 else "blocked"
        if status == "ready":
            collector.record_output(1)
        
        # Entry/stop/target calculation
        entry_price = bars[-1]['close']
        stop_price = bars.min()['low'] * 0.99
        target_price = entry_price + (entry_price - stop_price) * 2.0
        
        emit_symbol_evaluation_detail(
            symbol=symbol,
            stage="levels_calculated",
            metrics={
                "entry": entry_price,
                "stop": stop_price,
                "target": target_price,
                "risk": entry_price - stop_price,
                "reward": target_price - entry_price,
                "reward_ratio": (target_price - entry_price) / (entry_price - stop_price)
            },
            passed=True
        )
        
        # Finalize telemetry
        metrics = collector.finalize()
        emit_module_tick(
            module_name="evaluation",
            stage="completed",
            metrics=metrics
        )
        
        return {
            'status': status,
            'score': score,
            'criteria': [
                {'name': name, 'passed': passed, 'value': value}
                for name, passed, value in criteria_results
            ],
            'entry': entry_price,
            'stop': stop_price,
            'target': target_price,
            'quality': quality
        }
        
    except Exception as e:
        collector.record_error()
        metrics = collector.finalize()
        emit_module_tick(
            module_name="evaluation",
            stage="failed",
            metrics=metrics,
            errors=[{"symbol": symbol, "message": str(e)}]
        )
        raise
```

**What you'll see in event store:**

```json
[
  {
    "type": "symbol_evaluation_detail",
    "symbol": "AAPL",
    "stage": "gap_checked",
    "metrics": {"gap_pct": 2.1, "threshold": 2.0},
    "passed": true
  },
  {
    "type": "symbol_evaluation_detail",
    "symbol": "AAPL",
    "stage": "rvol_calculated",
    "metrics": {"rvol": 1.2, "threshold": 1.5, "current_5min_vol": 85000},
    "passed": false,
    "blocking_reason": "RVOL 1.20x < 1.5x"
  },
  {
    "type": "symbol_evaluation_detail",
    "symbol": "AAPL",
    "stage": "bull_flag_detected",
    "metrics": {"confidence": 0.75, "threshold": 0.5},
    "passed": true
  },
  {
    "type": "symbol_evaluation_detail",
    "symbol": "AAPL",
    "stage": "score_calculated",
    "metrics": {"score": 48.0, "criteria_passed": 4, "total_criteria": 9, "threshold": 60},
    "passed": false,
    "blocking_reason": "Score 48.0% < 60%"
  },
  {
    "type": "module_tick",
    "module": "evaluation",
    "stage": "completed",
    "duration_ms": 1823,
    "input_count": 5,
    "output_count": 2,
    "metrics": {
      "avg_score": 58.3,
      "ready_count": 2,
      "blocked_count": 3
    }
  }
]
```

---

## Example 3: Updating `run_live_paper.py`

### Small changes needed
```python
from core.telemetry import TelemetryEmitter, set_telemetry_emitter
from storage.event_store import EventStore

def build_runtime(args: argparse.Namespace) -> dict:
    """Wire up every component; safe without API keys."""
    
    # ... existing code ...
    
    event_store = EventStore()
    
    # NEW: Initialize telemetry emitter
    emitter = TelemetryEmitter(event_store=event_store)
    set_telemetry_emitter(emitter)  # Make it global
    
    # ... rest of existing code ...
    
    return {
        'event_store': event_store,
        'emitter': emitter,  # NEW
        # ... other components ...
    }
```

That's it! The modules will use the global emitter automatically.

---

## Example 4: Instrumenting `TradingExecutionService`

```python
def tick(self):
    """Execute one tick with telemetry."""
    
    collector = TelemetryCollector("execution")
    
    # Check for new signal_ready events
    ready_signals = query_ready_signals_snapshot(self.store)
    collector.record_input(len(ready_signals))
    
    for signal in ready_signals:
        try:
            # Risk checks
            open_pos = self._open_position_count()
            if open_pos >= self.settings.max_concurrent_positions:
                emit_symbol_evaluation_detail(
                    symbol=signal.symbol,
                    stage="risk_check_max_positions",
                    metrics={"open_positions": open_pos, "max": self.settings.max_concurrent_positions},
                    passed=False,
                    blocking_reason=f"Max positions {open_pos} >= limit {self.settings.max_concurrent_positions}"
                )
                continue
            
            # Position sizing
            shares = calculate_position_size(
                entry=signal.entry,
                stop=signal.stop,
                equity=self.equity,
                risk_pct=self.settings.risk_per_trade_pct
            )
            
            # Emit approval request
            self.store.emit(OrderApprovalRequestedEvent(...))
            collector.record_output(1)
            
        except Exception as e:
            collector.record_error()
    
    metrics = collector.finalize()
    emit_module_tick(
        module_name="execution",
        stage="completed",
        metrics=metrics
    )
```

---

## Time Breakdown for Phase 2

| Task | Time |
|------|------|
| Instrument ingestion module | 30 mins |
| Instrument research module | 30 mins |
| Instrument evaluation module | 60 mins (most complex) |
| Instrument execution module | 45 mins |
| Instrument broker sync module | 30 mins |
| Update run_live_paper.py | 15 mins |
| Test & verify | 45 mins |
| **Total** | **~4 hours** |

---

## Verification Checklist for Phase 2

After completing Phase 2, you should be able to:

```bash
# Run the system
python run_live_paper.py --once

# Check event store has new events
sqlite3 data/momentum_events.duckdb "SELECT COUNT(*), event_type FROM events GROUP BY event_type;"

# Expected output:
# 5 | module_tick
# 3 | data_validation
# 15 | symbol_evaluation_detail
# ... (plus existing event types)

# View a symbol's complete journey
sqlite3 data/momentum_events.duckdb \
  "SELECT event_type, symbol, stage, passed FROM events \
   WHERE symbol='AAPL' AND event_type IN ('symbol_evaluation_detail', 'data_validation') \
   ORDER BY timestamp;"

# Expected output:
# symbol_evaluation_detail | AAPL | gap_checked | 1
# symbol_evaluation_detail | AAPL | rvol_calculated | 0
# symbol_evaluation_detail | AAPL | bull_flag_detected | 1
# ...
```

---

## Key Points

1. **Minimal changes to existing code** — mostly wrapping with telemetry
2. **No breaking changes** — all existing functionality preserved
3. **Backward compatible** — code still works without telemetry
4. **Testable** — each instrumented module still works alone
5. **Progressive** — instrument one module at a time, test, then move to next

---

## What Phase 3 Needs

Once Phase 2 is done, Phase 3 will:
- Query the `symbol_evaluation_detail` events for symbol inspector
- Query `module_tick` events for module performance grid
- Query `data_validation` events for quality report

Everything needed will be in the event store already!
