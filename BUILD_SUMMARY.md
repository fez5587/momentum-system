# Complete Build Summary & Implementation Roadmap

## What You Asked For

> "I can't see what's happening. I don't know if bars are being collected. I can't verify research data is accurate. I can't test each module independently. The UI doesn't help debug. And I need a better visualization."

---

## What Was Built (Phase 1 ✅)

### Core Infrastructure

**1. Module Contracts** (`core/contracts.py`)
- Defined 5 typed I/O contracts (ingestion, research, evaluation, execution, broker)
- Every module now has explicit `XyzRequest` (input) and `XyzResult` (output) types
- IDE autocomplete friendly
- Makes bugs obvious when contracts don't match

**2. Telemetry Collection** (`core/telemetry.py`)
- `TelemetryCollector`: Record metrics during module execution
- `TelemetryEmitter`: Emit events to event store + callbacks
- `ModuleMetrics`: Standard metrics (duration, counts, custom fields)
- Global emitter instance for easy access

**3. Data Validation** (`core/validation.py`)
- `SchemaValidator`: Check required fields and types
- `RangeValidator`: Numeric bounds checking
- `ChecksumValidator`: Data integrity verification
- `BusinessRuleValidator`: Custom validation logic
- `BarDataValidator`: Pre-built OHLCV validation

**4. New Event Types** (`storage/event_schema.py`, extended)
- `ModuleTickEvent`: Module execution telemetry
- `DataValidationEvent`: Data quality checkpoints
- `SymbolEvaluationDetailEvent`: Deep-dive symbol metrics

---

## Complete 8-Phase Implementation Plan

### ✅ Phase 1: Core Infrastructure (DONE)
**Time:** ~2 hours  
**Status:** Complete

**Deliverables:**
- Core contracts and validation framework
- Telemetry collection and emission
- 3 new event types in event store
- Documentation of all new types

**Why it matters:** Foundation for everything else. Modules can now be transparent.

---

### 📍 Phase 2: Module Instrumentation (NEXT)
**Estimated time:** ~4 hours  
**What it does:** Add telemetry to existing modules

**Work:**
1. Wrap each module with `TelemetryCollector`
2. Emit `ModuleTickEvent` on completion
3. Add validation checkpoints with `DataValidationEvent`
4. Emit `SymbolEvaluationDetailEvent` during evaluation
5. Update `run_live_paper.py` to initialize telemetry

**Example output:**
```
ModuleTickEvent: ingestion completed in 245ms, 500 rows
ModuleTickEvent: research completed in 156ms, 5 symbols
SymbolEvaluationDetailEvent: AAPL gap_checked (2.1%)
SymbolEvaluationDetailEvent: AAPL rvol_calculated (1.2x)
DataValidationEvent: evaluation schema valid
```

**Files to modify:**
- `research/ingestion/market_data.py` — add ingestion telemetry
- `research/module.py` — add research telemetry
- `strategy/evaluation/setup_evaluator.py` — add evaluation telemetry + detail events
- `trading_execution.py` — add execution telemetry
- `alpaca_paper/sync.py` — add broker sync telemetry
- `run_live_paper.py` — initialize emitter, pass to modules

**Tests:** Run existing tests to ensure telemetry doesn't break anything

---

### 🔲 Phase 3: Inspection Dashboard API
**Estimated time:** ~6 hours  
**What it does:** Build backend REST API for module data

**Files to create:**
- `api/inspection.py` — new API endpoints

**Endpoints:**
```
GET  /api/inspection/modules              → module metrics table
GET  /api/inspection/pipeline              → pipeline flow stats
GET  /api/inspection/symbol/:symbol        → detailed eval timeline
GET  /api/inspection/data-quality          → validation report
GET  /api/inspection/events               → SSE stream of telemetry
GET  /api/inspection/module/:module/history → last N executions
```

**Projections to build** (`storage/projections.py`, add new functions):
```python
def query_module_performance() → [{module, avg_latency, last_run, error_rate}]
def query_symbol_evaluation_detail(symbol) → {stages with metrics}
def query_data_quality_snapshot() → {validation stats, freshness}
def query_pipeline_stats(session_id) → {module timings, input/output counts}
```

**Tests:**
- Test each endpoint returns correct data
- Test projections handle missing data gracefully
- Test SSE streaming with multiple clients

---

### 🔲 Phase 4: Web Dashboard Frontend
**Estimated time:** ~8 hours  
**What it does:** Build React SPA for visualization

**Files to create:**
```
api/inspection/
├── index.html                 # Single-page app shell
├── app.js                     # React app entry
├── components/
│   ├── PipelineDiagram.js    # Mermaid auto-layout diagram
│   ├── ModuleGrid.js         # Performance metrics table
│   ├── SymbolInspector.js    # Search + expandable criteria
│   ├── EventTimeline.js      # Live event log with filtering
│   └── DataQualityReport.js  # Validation statistics
└── styles/
    └── dashboard.css          # Responsive styling
```

**Features:**
- Real-time SSE updates
- Interactive symbol search
- Expandable evaluation stages
- Sparkline charts (recharts)
- Event timeline with filtering
- Data quality heatmap
- Mobile responsive

**Design:**
- Dark theme (consistent with trading terminals)
- One symbol per row, expandable details
- Color coding (✓ green, ✗ red, ? gray)
- Click to drill down into metrics

**Tests:**
- Component rendering tests
- SSE connection/fallback tests
- Real-world data from phase 3

---

### 🔲 Phase 5: Module Refactoring
**Estimated time:** ~6 hours  
**What it does:** Modularize existing code into explicit contracts

**Files to create:**
```
modules/
├── ingestion.py    # BarIngestRequest → BarIngestResult
├── research.py     # ResearchRequest → ResearchResult
├── evaluation.py   # EvaluationRequest → EvaluationResult
├── execution.py    # ExecutionRequest → ExecutionResult
└── broker.py       # BrokerRequest → BrokerResult
```

**Pattern (example for evaluation):**
```python
class EvaluationModule:
    def run(self, request: EvaluationRequest) -> EvaluationResult:
        """Pure function: request → result."""
        # Validate input
        is_valid, failures = self.validate_input(request)
        
        # Execute strategy logic
        result = evaluate_setup(
            bars_df=request.bars_df,
            symbol=request.symbol,
            previous_close=request.previous_close,
            avg_daily_volume=request.avg_daily_volume
        )
        
        # Return strongly-typed result
        return EvaluationResult(
            symbol=request.symbol,
            status=result['status'],
            score=result['score'],
            criteria=result['criteria'],
            validation_failures=failures
        )
```

**Benefits:**
- Each module is a pure function: `input → output`
- Can test in isolation
- Can swap implementations (real vs. mock)
- Dependency injection friendly

**Dependency injection (trait-style):**
```python
class EvaluationModule:
    def __init__(self, research_db: ResearchDB = None, ...):
        self.research_db = research_db or RealResearchDB()
    
    # In tests:
    module = EvaluationModule(research_db=MockResearchDB())
```

**Tests:**
- Unit test each module in isolation
- Integration test module chain
- Contract compliance tests

---

### 🔲 Phase 6: Module Testing Framework
**Estimated time:** ~5 hours  
**What it does:** Write comprehensive test suite

**Files to create:**
```
tests/
├── modules/
│   ├── conftest.py                           # fixtures
│   ├── test_ingestion_module.py              # 8 tests
│   ├── test_research_module.py               # 6 tests
│   ├── test_evaluation_module.py             # 10 tests
│   ├── test_execution_module.py              # 8 tests
│   ├── test_broker_module.py                 # 8 tests
│   └── fixtures/
│       ├── bull_flag_bars.py                 # Synthetic test data
│       ├── fading_bars.py
│       ├── gap_up_bars.py
│       └── low_volume_bars.py
│
├── integration/
│   ├── test_pipeline_e2e.py                  # Full flow test
│   ├── test_signal_flow.py                   # Signal lifecycle
│   └── test_event_consistency.py             # Audit trail
│
└── scenarios/
    ├── bull_flag_setup.py                    # Golden path
    ├── fading_tape_blocked.py                # Negative case
    └── risk_rule_rejection.py                # Execution blocker
```

**Test patterns:**
```python
def test_evaluation_module_ready():
    """Module accepts input, produces output."""
    module = EvaluationModule()
    request = EvaluationRequest(
        symbol="AAPL",
        bars_df=bull_flag_bars(),
        previous_close=150.0,
        avg_daily_volume=1_000_000,
        session_date=date(2026, 6, 15)
    )
    
    result = module.run(request)
    
    assert result.status == "ready"
    assert result.score >= 60
    assert result.entry_price > 0
    # ...

def test_evaluation_module_dry_run():
    """Module works without database."""
    module = EvaluationModule(research_db=MockResearchDB())
    # Same test as above, but doesn't touch real DB
    
def test_evaluation_module_validation():
    """Module validates input, captures errors."""
    module = EvaluationModule()
    request = EvaluationRequest(
        symbol="AAPL",
        bars_df=invalid_bars(),  # Zero volume bar
        # ...
    )
    
    result = module.run(request)
    
    assert not result.success
    assert len(result.validation_failures) > 0
```

**Run tests:**
```bash
pytest tests/modules/                   # All module tests
pytest tests/modules/test_evaluation_module.py -v  # Specific module
pytest tests/integration/                # Full pipeline
```

---

### 🔲 Phase 7: Dry-Run Mode & Synthetic Data
**Estimated time:** ~4 hours  
**What it does:** Add `--dry-run` flag for testing without APIs

**Files to create:**
```
tests/
├── synthetic.py                        # Bar generators
│   ├── bull_flag_bars()               # Ideal setup
│   ├── fading_bars()                  # Will block
│   ├── gap_up_bars()                  # Gap but low volume
│   └── spike_bars()                   # Volume spike
│
└── providers/
    ├── mock_research_db.py            # In-memory research DB
    ├── mock_broker.py                 # Simulated fills
    └── mock_bars_provider.py          # Synthetic bars

core/
└── test_mode.py                       # DryRun context manager
```

**Synthetic data generators:**
```python
def bull_flag_bars(high_price=150.0, low_price=145.0, bars=50):
    """Generate realistic bull-flag OHLCV bars."""
    # - Gap up on open
    # - Initial breakout
    # - Pullback formation
    # - Higher lows pattern
    # High volume throughout
    # Returns: DataFrame
```

**Usage:**
```bash
# Dry-run mode: no API keys, no real orders
python run_live_paper.py --dry-run \
  --symbols AAPL,TSLA,AMD \
  --bars-provider synthetic:bull_flag \
  --num-ticks 5 \
  --inspection-dashboard

# Output:
# Tick 1: ingestion ✓, research ✓, evaluation ✓ (2 ready)
# Tick 2: execution ✓ (approved), broker ✓ (simulated fills)
# ...all visible in inspection dashboard
```

**Key benefits:**
- Deterministic test runs (same data every time)
- No API rate limits
- No need for live market hours
- Test configuration changes instantly
- Use in CI/CD for regression testing

---

### 🔲 Phase 8: Rich TUI (Optional)
**Estimated time:** ~3 hours  
**What it does:** Terminal UI for SSH/CI/CD environments

**Files to create:**
```
tui/
├── dashboard.py            # Rich-based TUI
└── components.py           # Reusable widgets
```

**Features:**
- Progress bars for each module (ingestion, research, eval, exec, broker)
- Live metrics table (latencies, counts, error rates)
- Event log (colorized by severity)
- Keyboard commands (pause, resume, copy logs)
- Works over SSH (no browser needed)

**Usage:**
```bash
python run_live_paper.py --tui
```

**Output:**
```
[Ingestion]    ████████░░░ 80% (400/500)
[Research]     ██████████ 100% (5/5)
[Evaluation]   ██████████ 100% (5/5 → 2 ready)
[Execution]    ██████░░░░ 60% (1/2 approved)
[Broker]       ████░░░░░░ 40% (2/5 submitted)

Recent Events:
  [09:35:15] ✓ TSLA evaluated, ready (score 78%)
  [09:35:14] ✗ AAPL blocked (rvol 1.2x < 1.5x)
  [09:35:13] ✓ Ingestion complete (500 rows)
```

---

## Implementation Sequence

**Week 1:**
- Phase 1: Core infrastructure ✅ (DONE)
- Phase 2: Module instrumentation (4-6 hours)

**Week 2:**
- Phase 3: Inspection API (6 hours)
- Phase 4: React dashboard (8 hours)

**Week 3:**
- Phase 5: Module refactoring (6 hours)
- Phase 6: Test framework (5 hours)

**Week 4:**
- Phase 7: Dry-run mode (4 hours)
- Phase 8: TUI (3 hours, optional)

**Total:** ~4 weeks for full implementation, or ~2 weeks for core (phases 1-4).

---

## Why This Solves Your Problems

| Problem | Solution | Phase |
|---------|----------|-------|
| Can't see bars being collected | ModuleTickEvent shows input/output counts + validation | 1 + 2 |
| Can't verify research data | DataValidationEvent + SymbolEvaluationDetailEvent | 1 + 2 |
| Can't test modules independently | Module contracts + mock dependencies | 5 + 6 |
| UI doesn't help debug | Inspection dashboard with symbol inspector | 3 + 4 |
| No visibility into evaluation steps | SymbolEvaluationDetailEvent for each stage | 1 + 2 |
| Can't dry-run without APIs | --dry-run + synthetic bars | 7 |

---

## Next Immediate Steps

### Phase 2 Kickoff (Start Now)

1. **Identify all modules to instrument:**
   - `research/ingestion/market_data.py` — `ingest_live_minute_bars()`
   - `research/module.py` — `get_candidates()`, `get_bars()`
   - `strategy/evaluation/setup_evaluator.py` — `evaluate_setup()`
   - `trading_execution.py` — `TradingExecutionService.tick()`
   - `alpaca_paper/sync.py` — `AlpacaPaperSync.sync_account()`

2. **For each module:**
   - Wrap with `TelemetryCollector(module_name)`
   - Record input/output counts
   - Add validation with `emit_data_validation()`
   - Emit `ModuleTickEvent` on completion
   - Update error handling to record errors

3. **Update `run_live_paper.py`:**
   - Initialize `TelemetryEmitter`
   - Pass to modules (or use global instance)
   - Test that existing behavior unchanged (should be!)

4. **Verify:**
   - Run `python run_live_paper.py --once`
   - Check event store has new events
   - Verify metrics are accurate

---

## Success Criteria

After Phase 4 complete, you should be able to:

✅ Open http://127.0.0.1:8011 (inspection dashboard)  
✅ See real-time module pipeline diagram  
✅ Search for a symbol and see complete evaluation journey  
✅ Understand exactly why a symbol is ready/blocked  
✅ See timestamp, metrics, and raw data for each stage  
✅ View live event stream  
✅ Access data quality report  

**Time to understand "why isn't AAPL signaling?":** < 10 seconds

---

## Files You Now Have

```
core/
├── __init__.py
├── contracts.py          ← Module I/O types
├── telemetry.py          ← Metric collection
└── validation.py         ← Data validation

storage/
└── event_schema.py       ← (extended) 3 new event types

Documentation/
├── PHASE1_IMPLEMENTATION.md     ← How Phase 1 works
├── INSPECTION_ROADMAP.md        ← Full system visibility
└── BUILD_SUMMARY.md             ← This file
```

---

## Questions?

Each phase has clear, testable deliverables. After Phase 2 (instrumenting existing modules), you'll start seeing telemetry events in your event store.

After Phase 4 (React dashboard), you'll have full visibility into your system.

The foundation is solid. Ready to move to Phase 2?
