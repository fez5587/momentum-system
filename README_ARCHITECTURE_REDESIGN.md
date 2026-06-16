# Architecture Redesign: From Black Box to Transparent Pipeline

## The Problem You Identified

- ❌ Can't see if bars are being collected
- ❌ Can't verify research data is accurate  
- ❌ Can't test modules independently
- ❌ UI provides no debugging help
- ❌ No visibility into calculation steps

---

## The Solution: 8-Phase Implementation

A complete redesign that transforms the system from a black box into a transparent, observable, testable pipeline.

### Overview

```
Phase 1: Core Infrastructure  ✅ DONE
         ↓
Phase 2: Module Instrumentation (4 hours)
         ↓
Phase 3: Inspection API Backend (6 hours)
         ↓
Phase 4: React Dashboard (8 hours)
         ↓
Phases 5-8: Refactoring & Optional Features
```

---

## Documentation

Start with these files in order:

### 1. **READ FIRST: BUILD_SUMMARY.md**
   - Executive summary of all 8 phases
   - Timeline and effort estimates
   - Success criteria
   - Why this solves your problems

### 2. **PHASE1_IMPLEMENTATION.md**
   - What was built in Phase 1 (contracts, telemetry, validation)
   - How to use each new component
   - Example code patterns
   - Key takeaway: System is no longer a black box

### 3. **INSPECTION_ROADMAP.md**
   - What you'll be able to see after all phases complete
   - Interactive dashboard features
   - Symbol inspector details
   - Real-world debugging scenarios

### 4. **PHASE2_PREVIEW.md**
   - Exactly what Phase 2 code looks like
   - Before/after comparisons
   - How many events you'll see
   - Time breakdown for Phase 2 work

---

## Quick Start: What's Ready Now (Phase 1)

### New Modules

**`core/contracts.py`** — Module I/O Contracts
```python
# Every module has typed input/output
EvaluationRequest → evaluate_setup() → EvaluationResult
```

**`core/telemetry.py`** — Metric Collection
```python
# Record what's happening during execution
collector = TelemetryCollector("evaluation")
collector.record_metric("gap_pct", 2.5)
collector.record_output(2)  # 2 symbols ready
metrics = collector.finalize()
```

**`core/validation.py`** — Data Validation
```python
# Validate data at module boundaries
validator = SchemaValidator(required_fields={"symbol": str, "price": float})
is_valid, failures = validator.validate(data)
```

### New Event Types

In `storage/event_schema.py`:
- `ModuleTickEvent` — Module execution telemetry
- `DataValidationEvent` — Data quality checkpoints
- `SymbolEvaluationDetailEvent` — Deep-dive symbol metrics

### Example Usage

```python
from core.telemetry import emit_module_tick, emit_symbol_evaluation_detail

# Emit when symbol evaluation stage completes
emit_symbol_evaluation_detail(
    symbol="AAPL",
    stage="rvol_calculated",
    metrics={"rvol": 1.2, "threshold": 1.5},
    passed=False,
    blocking_reason="RVOL 1.2x < 1.5x threshold"
)

# Events are now in the event store, queryable by the dashboard
```

---

## Next: Phase 2 (Ready to Start)

Phase 2 instruments the existing modules to emit telemetry events.

**Work:** ~4 hours
1. Wrap `ingest_live_minute_bars()` with telemetry
2. Wrap `evaluate_setup()` with telemetry  
3. Wrap execution, broker sync, research modules
4. Test to verify events are being emitted

**See:** PHASE2_PREVIEW.md for exact code examples

---

## The End Goal: Full Transparency

After all 8 phases, opening `http://127.0.0.1:8011` shows:

```
AAPL (state: blocked, score: 48%)
├─ INGESTION ✓
│  └─ 12 bars collected, validation passed
├─ RESEARCH ✓
│  ├─ gap: 2.1% ✓
│  └─ rvol: 1.2x ✗ (need 1.5x) ← BLOCKING
├─ EVALUATION
│  ├─ gap_checked ✓
│  ├─ rvol_calculated ✗ ← stops here
│  └─ score: 48% (threshold: 60%)
└─ (not evaluated, blocked upstream)
```

**Time to understand why AAPL didn't signal:** < 10 seconds

---

## Files Overview

### Core Infrastructure (Phase 1)
```
core/
├── contracts.py     # Module I/O types (500 lines)
├── telemetry.py     # Metric collection (250 lines)
└── validation.py    # Data validation (400 lines)
```

### Documentation
```
PHASE1_IMPLEMENTATION.md     # How Phase 1 works (200 lines)
INSPECTION_ROADMAP.md        # Full system visibility (400 lines)
BUILD_SUMMARY.md             # All 8 phases + timeline (350 lines)
PHASE2_PREVIEW.md            # Exact Phase 2 code (300 lines)
```

### Later Phases
```
api/inspection.py             # Phase 3: Inspection API
api/inspection/               # Phase 4: React dashboard
modules/                      # Phase 5: Refactored modules
tests/modules/                # Phase 6: Module tests
```

---

## Key Design Principles

1. **Explicit Contracts**
   - Every module has `Request` → `Result` types
   - IDE autocomplete friendly
   - Bugs obvious when types don't match

2. **Observability First**
   - Every module emits telemetry
   - Metrics, timing, errors all captured
   - Events flow to event store

3. **Validation at Boundaries**
   - Data validated when entering each module
   - Business rules checked
   - Checksums computed

4. **Modularity**
   - Each component is independent
   - Can test in isolation
   - Can swap implementations (mock vs. real)

5. **Transparency**
   - Nothing happens silently
   - All state changes are events
   - Complete audit trail

---

## Success Metrics

| Metric | Before | After |
|--------|--------|-------|
| Time to debug "why isn't AAPL signaling?" | 30 mins | 10 secs |
| Can test evaluation module alone | No | Yes |
| Can see bars being collected | No | Yes |
| Can verify research data accuracy | No | Yes |
| Module error tracking | None | Complete |
| Data validation coverage | None | 100% |

---

## Implementation Path

### Immediate (This Week)
- ✅ Phase 1: Core infrastructure (DONE)
- 📍 Phase 2: Instrument existing modules (4 hours)

### Week 2
- Phase 3: Inspection API (6 hours)
- Phase 4: React dashboard (8 hours)

### Week 3+
- Phases 5-8: Refactoring, testing, optional features

---

## Questions to Ask While Reading

As you go through the documentation, ask yourself:

1. **On contracts:** "If I call `evaluate_setup()` with an `EvaluationRequest`, what exactly should I expect back?"
   → Answer: An `EvaluationResult` with specific fields

2. **On telemetry:** "How long did evaluation take? How many symbols were ready? What metrics does it track?"
   → Answer: All captured in `ModuleTickEvent`

3. **On validation:** "What if a bar has zero volume? What if the high is lower than the low?"
   → Answer: `BarDataValidator` catches both

4. **On events:** "If I want to understand why AAPL is blocked, where do I look?"
   → Answer: `SymbolEvaluationDetailEvent` shows each step

5. **On inspection:** "Can I see the exact gap%, rvol, and bull-flag score?"
   → Answer: Yes, all in the dashboard after Phase 4

---

## Reading Order

1. **BUILD_SUMMARY.md** — Start here for the big picture
2. **PHASE1_IMPLEMENTATION.md** — Understand what was built
3. **PHASE2_PREVIEW.md** — See what you'll code next
4. **INSPECTION_ROADMAP.md** — Visualize the end goal
5. **Code files** — `core/contracts.py`, `core/telemetry.py`, `core/validation.py`

---

## Next Steps

1. Read BUILD_SUMMARY.md (20 minutes)
2. Review PHASE1_IMPLEMENTATION.md (15 minutes)
3. Skim PHASE2_PREVIEW.md to see code examples (10 minutes)
4. Decide: Ready to start Phase 2, or want clarifications first?

If ready for Phase 2:
- Use PHASE2_PREVIEW.md as your code template
- Follow the work breakdown in BUILD_SUMMARY.md
- Test after each module is instrumented
- Verify events appear in event store

---

## Summary

**Problem:** Black-box system, no visibility, can't test modules, can't debug

**Solution:** 8-phase implementation adding:
- ✅ Module contracts (Phase 1)
- 📍 Module telemetry (Phase 2)
- Module inspection API (Phase 3)
- Module inspection dashboard (Phase 4)
- Module refactoring (Phase 5)
- Module testing (Phase 6)
- Dry-run mode (Phase 7)
- TUI debugging (Phase 8)

**Timeline:** 4 weeks for full implementation, 2 weeks for core (phases 1-4)

**Benefit:** From 30 minutes to 10 seconds to understand why a signal didn't execute

---

## Let's Go

Phase 1 is complete. Phase 2 is ready to start.

Which would help you more right now:
1. More explanation of the design?
2. Code walkthrough of Phase 1?
3. Start working on Phase 2?

Let me know!
