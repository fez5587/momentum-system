# Phase 1: Module Contracts & Telemetry Infrastructure

## What Was Built

Phase 1 establishes the **foundation** for modular, observable trading system. Instead of black-box components, every module now has:

1. **Explicit I/O contracts** — standardized input/output types
2. **Telemetry collection** — metrics, timing, error tracking
3. **Data validation** — schema checks, business rule validation, checksums
4. **Event types** — new event schema for module observability

This makes the system **transparent**: you can see exactly what data is flowing where, how long each step takes, and what validation passed/failed.

---

## New Modules

### `core/contracts.py` — Module I/O Contracts

Defines TypeScript-like contracts for every module:

```python
# Every module accepts typed input and produces typed output

BarIngestRequest          ResearchRequest          EvaluationRequest
    ↓                           ↓                         ↓
BarIngestResult          ResearchResult          EvaluationResult
```

**Key classes:**

- `ModuleMetrics` — Standard metrics every module tracks
  - `duration_ms`: How long the module took
  - `input_count`, `output_count`: Items processed
  - `error_count`, `warnings`: Quality metrics
  - `custom_metrics`: Module-specific (gap%, rvol, etc.)

- `ModuleResult` — Base class for all module outputs
  - `success`: Did it work?
  - `validation_failures`: What went wrong?
  - `error_summary()`: Human-readable error message

- **Five module contracts** (ingestion, research, evaluation, execution, broker)
  - Each has `XyzRequest` (input) and `XyzResult` (output)
  - Strongly typed, IDE-friendly
  - Makes contracts explicit

**Usage:**

```python
from core.contracts import EvaluationRequest, EvaluationResult

# Module accepts strongly-typed input
request = EvaluationRequest(
    symbol="AAPL",
    bars_df=my_dataframe,
    previous_close=150.0,
    avg_daily_volume=1_000_000,
    session_date=date(2026, 6, 15)
)

# Module produces strongly-typed output
result: EvaluationResult = evaluate_setup(request)

# Safe to access fields — IDE autocomplete works
if result.status == "ready":
    print(f"Entry: ${result.entry_price}")
    for criterion in result.criteria:
        print(f"  {criterion.name}: {'✓' if criterion.passed else '✗'}")
```

---

### `core/telemetry.py` — Metric Collection & Emission

Real-time telemetry collection and event emission.

**Key classes:**

- `TelemetryCollector` — Collect metrics during module execution
  ```python
  collector = TelemetryCollector("evaluation")
  
  # Record what you're processing
  collector.record_input(count=5)  # evaluated 5 symbols
  
  # Record custom metrics
  collector.record_metric("gap_pct", 2.5)
  collector.record_metric("rvol", 1.8)
  
  # Finalize when done
  metrics = collector.finalize()
  # → ModuleMetrics with duration_ms, counts, custom metrics
  ```

- `TelemetryEmitter` — Emit events to event store
  ```python
  emitter = get_telemetry_emitter()
  
  # Emit module execution telemetry
  emitter.emit_module_tick(
      module_name="evaluation",
      stage="completed",
      metrics=metrics,
      correlation_id="session-123"
  )
  
  # Emit data validation results
  emitter.emit_data_validation(
      module_name="ingestion",
      validation_type="schema",
      valid=True,
      details={
          "rows_validated": 500,
          "failures": []
      }
  )
  
  # Emit detailed symbol evaluation metrics
  emitter.emit_symbol_evaluation_detail(
      symbol="AAPL",
      stage="gap_checked",
      metrics={"gap_pct": 2.5, "threshold": 2.0},
      passed=True
  )
  ```

- **Callbacks** — Emit events to multiple destinations
  ```python
  emitter = get_telemetry_emitter()
  
  # Add callback to receive all telemetry events
  emitter.add_callback(lambda event: logger.info(f"Event: {event['type']}"))
  
  # When you emit, all callbacks are called
  emitter.emit_module_tick(...)
  ```

---

### `core/validation.py` — Data Validation

Multi-layer validation at module boundaries.

**Validators:**

1. **SchemaValidator** — Check required/optional fields and types
   ```python
   validator = SchemaValidator(
       required_fields={"symbol": str, "qty": int, "price": float},
       optional_fields={"notes": str}
   )
   
   is_valid, failures = validator.validate({"symbol": "AAPL", "qty": 100, "price": 150.0})
   # failures = []  (if valid)
   # or failures = [{"field": "qty", "reason": "Expected int, got str", ...}]
   ```

2. **RangeValidator** — Numeric bounds checking
   ```python
   rvol_validator = RangeValidator(min_val=1.0, max_val=10.0)
   is_valid, failure = rvol_validator.validate(1.5, field_name="rvol")
   ```

3. **ChecksumValidator** — Data integrity verification
   ```python
   data = {"bars": [...]100 items...}
   checksum = ChecksumValidator.compute_checksum(data)
   # checksum = "a1b2c3d4e5f6..." (MD5)
   
   # Later, verify data hasn't changed
   is_valid, failure = ChecksumValidator.validate_checksum(data, checksum)
   ```

4. **BusinessRuleValidator** — Custom logic validation
   ```python
   validator = BusinessRuleValidator()
   
   # Add custom rules
   validator.add_rule(
       "min_high_price",
       lambda data: (data["high"] > data["low"], "high must be > low")
   )
   
   is_valid, failures = validator.validate(bar_data)
   ```

5. **BarDataValidator** — Pre-built OHLCV validation
   ```python
   is_valid, failures = BarDataValidator.validate_bar({
       "timestamp": "2026-06-15 09:31:00",
       "open": 150.0,
       "high": 151.0,
       "low": 149.5,
       "close": 150.5,
       "volume": 50000
   })
   # Checks:
   # - All required fields present
   # - High ≥ open, close, low
   # - Low ≤ open, close, high
   # - Volume non-negative
   # - Warns on zero volume
   ```

---

## New Event Types

Added to `storage/event_schema.py`:

### 1. `ModuleTickEvent` — Module Execution Telemetry

Emitted by each module at the end of each tick.

```python
@dataclass
class ModuleTickEvent(BaseEvent):
    module: str          # "ingestion", "research", "evaluation", etc.
    stage: str          # "started", "processing", "completed", "failed"
    duration_ms: float  # How long it took
    input_count: int    # Items processed
    output_count: int   # Items produced
    metrics: dict       # Custom module metrics
    errors: list[dict]  # Any errors that occurred
```

**Example:**
```json
{
  "type": "module_tick",
  "module": "evaluation",
  "stage": "completed",
  "duration_ms": 245.5,
  "input_count": 5,
  "output_count": 2,
  "metrics": {
    "avg_score": 58.3,
    "ready_count": 2,
    "blocked_count": 3
  },
  "errors": [],
  "timestamp": "2026-06-15T09:35:00"
}
```

### 2. `DataValidationEvent` — Data Quality Checkpoint

Emitted when module validates data at boundaries.

```python
@dataclass
class DataValidationEvent(BaseEvent):
    module: str            # Module that validated
    validation_type: str   # "schema", "checksum", "business_rules"
    valid: bool           # Did it pass?
    details: dict         # Validation specifics
```

**Example:**
```json
{
  "type": "data_validation",
  "module": "ingestion",
  "validation_type": "schema",
  "valid": true,
  "details": {
    "rows_validated": 500,
    "rows_failed": 0,
    "failures": []
  }
}
```

### 3. `SymbolEvaluationDetailEvent` — Deep-Dive Symbol Metrics

Emitted during symbol evaluation to show intermediate calculations.

```python
@dataclass
class SymbolEvaluationDetailEvent(BaseEvent):
    symbol: str             # Symbol being evaluated
    stage: str             # "bars_loaded", "gap_checked", "rvol_calculated", etc.
    metrics: dict          # Metrics at this stage
    passed: bool           # Did this stage pass?
    blocking_reason: str   # Why it failed (if applicable)
```

**Example:**
```json
{
  "type": "symbol_evaluation_detail",
  "symbol": "AAPL",
  "stage": "gap_checked",
  "metrics": {
    "gap_pct": 2.5,
    "threshold": 2.0
  },
  "passed": true,
  "blocking_reason": null,
  "timestamp": "2026-06-15T09:35:12"
}
```

```json
{
  "type": "symbol_evaluation_detail",
  "symbol": "AAPL",
  "stage": "rvol_calculated",
  "metrics": {
    "rvol": 1.2,
    "threshold": 1.5,
    "current_5min_volume": 125000,
    "avg_20day_volume": 1000000
  },
  "passed": false,
  "blocking_reason": "RVOL 1.2x below threshold 1.5x",
  "timestamp": "2026-06-15T09:35:14"
}
```

---

## Usage Pattern: Instrumenting an Existing Module

Here's how to add telemetry to any module (e.g., evaluation):

```python
from core.telemetry import TelemetryCollector, emit_module_tick, emit_symbol_evaluation_detail
from core.validation import BarDataValidator

def evaluate_setup(bars_df, symbol, previous_close, avg_daily_volume):
    """Evaluate setup with full telemetry and validation."""
    
    collector = TelemetryCollector("evaluation")
    
    try:
        # Validate input
        bars = bars_df.to_dict('records')
        for i, bar in enumerate(bars):
            is_valid, failures = BarDataValidator.validate_bar(bar)
            if not is_valid:
                collector.record_error()
                emit_symbol_evaluation_detail(
                    symbol=symbol,
                    stage="bars_validated",
                    metrics={"valid_bars": i, "total_bars": len(bars)},
                    passed=False,
                    blocking_reason=f"Bar {i} invalid: {failures[0]['reason']}"
                )
                return EvaluationResult(success=False, metrics=collector.finalize())
        
        collector.record_input(len(bars))
        
        # Emit validation checkpoint
        emit_data_validation(
            module_name="evaluation",
            validation_type="schema",
            valid=True,
            details={"rows_validated": len(bars), "failures": []}
        )
        
        # Stage 1: Gap check
        gap_pct = ((bars[-1]['open'] - previous_close) / previous_close) * 100
        emit_symbol_evaluation_detail(
            symbol=symbol,
            stage="gap_checked",
            metrics={"gap_pct": gap_pct, "threshold": 2.0},
            passed=gap_pct >= 2.0
        )
        collector.record_metric("gap_pct", gap_pct)
        
        # Stage 2: RVOL check
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
            blocking_reason=None if rvol_passed else f"RVOL {rvol:.2f}x below 1.5x"
        )
        collector.record_metric("rvol", rvol)
        
        # ... more stages ...
        
        # Finalize
        collector.record_output(1)
        metrics = collector.finalize()
        
        # Emit module completion
        emit_module_tick(
            module_name="evaluation",
            stage="completed",
            metrics=metrics
        )
        
        return EvaluationResult(
            success=True,
            metrics=metrics,
            symbol=symbol,
            status="ready",
            score=score
        )
        
    except Exception as e:
        collector.record_error()
        metrics = collector.finalize()
        emit_module_tick(
            module_name="evaluation",
            stage="failed",
            metrics=metrics,
            errors=[{"message": str(e)}]
        )
        return EvaluationResult(success=False, metrics=metrics)
```

---

## Why This Matters

### Before (Black Box)
```
Bars ingested → [silence] → Signal ready
                ❓ Were bars actually valid?
                ❓ Which criterion failed?
                ❓ How long did it take?
                ❓ What was the gap%?
```

### After (Transparent)
```
ModuleTickEvent:
  module: "ingestion"
  input_count: 500
  output_count: 500
  metrics: {
    "rows_inserted": 500,
    "validation_failures": 0
  }

DataValidationEvent:
  module: "ingestion"
  validation_type: "schema"
  valid: true

SymbolEvaluationDetailEvent (AAPL):
  stage: "gap_checked"
  metrics: {"gap_pct": 2.5}
  passed: true

SymbolEvaluationDetailEvent (AAPL):
  stage: "rvol_calculated"
  metrics: {"rvol": 1.2}
  passed: false
  blocking_reason: "RVOL 1.2x below 1.5x"
```

---

## Next Steps

**Phase 2** will instrument the existing modules (ingestion, research, evaluation, execution, broker) to emit these events.

**Phase 3** will build the Inspection Dashboard API to query and visualize these events.

**Phase 4** will build the React frontend to make the data human-readable.

**Phase 7** will add `--dry-run` mode so you can test with synthetic data.

---

## Files Created

```
core/
├── __init__.py                # Empty init
├── contracts.py               # Module I/O contracts (5 modules)
├── telemetry.py               # Metric collection & emission
└── validation.py              # Multi-layer validation

storage/
└── event_schema.py            # (MODIFIED) Added 3 new event types
```

---

## Key Takeaway

The system is no longer a black box. Every module now:

1. ✅ Has **explicit contracts** (input/output types)
2. ✅ **Emits telemetry** (duration, counts, metrics)
3. ✅ **Validates data** at module boundaries
4. ✅ **Reports errors** with context

This foundation enables the inspection dashboard, dry-run mode, and per-module testing to all work correctly.
