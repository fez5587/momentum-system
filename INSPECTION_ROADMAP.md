# Inspection & Debugging Roadmap

## What You'll Be Able to See (When All Phases Complete)

Right now, when you run `python run_live_paper.py`, you see:
- ❌ No indication bars are being collected
- ❌ No visibility into research data
- ❌ Symbols appear/disappear silently
- ❌ No way to test individual modules
- ❌ Can't verify calculation accuracy

**After Phases 1-4, you'll have:**

---

## 1. Real-Time Module Dashboard (Port 8011)

Open `http://127.0.0.1:8011` to see live module execution:

### Data Pipeline Diagram (Auto-Generated)

```
┌──────────────┐    ┌──────────────┐    ┌────────────────┐    ┌───────────────┐
│ INGESTION    │───→│ RESEARCH     │───→│ EVALUATION     │───→│ EXECUTION     │
├──────────────┤    ├──────────────┤    ├────────────────┤    ├───────────────┤
│ 500 rows in  │    │ 5 researched │    │ 5 evaluated    │    │ 2 approved    │
│ 245ms        │    │ 156ms        │    │ 1823ms         │    │ 89ms          │
│ ✓ validated  │    │ ✓ all found  │    │ 2 ready, 3 blocked    │ 0 rejected    │
└──────────────┘    └──────────────┘    └────────────────┘    └───────────────┘
```

Each box shows:
- **Input count** → "500 rows in"
- **Latency** → "245ms"
- **Status** → "✓ validated"
- **Click to expand** → see detailed metrics

### Module Performance Grid

```
┌───────────┬──────────────┬──────────┬────────────────┬─────────────────┐
│ Module    │ Last Run (s) │ Avg (ms) │ Error Rate     │ Input → Output  │
├───────────┼──────────────┼──────────┼────────────────┼─────────────────┤
│ Ingestion │ 2.3s ago     │ 245      │ 0.0%           │ 500 → 500       │
│ Research  │ 2.1s ago     │ 156      │ 0.0%           │ 5 → 5           │
│ Evaluation│ 0.8s ago     │ 1823     │ 0.0%           │ 5 → 2           │
│ Execution │ 0.5s ago     │ 89       │ 0.0%           │ 2 → 2           │
│ Broker    │ 0.3s ago     │ 342      │ 0.0%           │ 2 → 2           │
└───────────┴──────────────┴──────────┴────────────────┴─────────────────┘
```

Click on each row to:
- See last 10 executions (histogram of latencies)
- View error log if failed
- Inspect metrics

---

## 2. Symbol Inspector (Search Box)

Type a symbol name to see its complete evaluation journey:

```
 🔍 Search symbol... [AAPL]
────────────────────────────────────────────────────────────────

AAPL (state: blocked, score: 48%)
├─ INGESTION ✓
│  ├─ bars collected: 12
│  ├─ validation: passed ✓
│  ├─ quality_score: 0.82
│  └─ collected_at: 09:35:12
│
├─ RESEARCH ✓
│  ├─ gaps: 2.1%
│  ├─ rvol: 1.2x ✗ (need 1.5x)
│  ├─ daily avg volume: 500k
│  ├─ previous close: $10.50
│  └─ researched_at: 09:35:13
│
├─ EVALUATION ✓ bars_loaded
│                ├─ gap_checked ✓ (2.1%)
│                ├─ rvol_calculated ✗ (1.2x < 1.5x) ← BLOCKING REASON
│                ├─ bull_flag ✓ (confidence 0.75)
│                ├─ 1st_candle ✗ (weak)
│                ├─ momentum ✓
│                ├─ quality: 0.62
│                ├─ score: 48% (threshold: 60%)
│                └─ evaluated_at: 09:35:15
│
├─ EXECUTION (blocked - not evaluated)
│  └─ blocked_reason: "criteria score 48% < 60%"
│
└─ BROKER (N/A - not submitted)
```

Click on each section to see:
- Exact metric values
- Why it passed/failed
- Timestamp of calculation
- Raw data (for debugging)

### Sparkline Chart (OHLCV)

```
AAPL: 09:30 → 09:35 (12 bars)

  11.00│                    ╭─╮
  10.80│          ╭─╮  ╭─╮╭─╯ ╰─╮
  10.60│ ╭─╮ ╭─╮ ╭─╯╰─╮
  10.40│╭─╯ ╰─╮
  10.20│╯
       └────────────────────────────
        Volume: avg=125k, last=85k
        
  Entry: $10.52 ━━━━━━━━━━━━━━
  Stop:  $10.20 ━━━━━━━━━━━━━━
```

Interactive:
- Hover over bars to see OHLCV
- Click to zoom in/out
- See entry/stop/target reference lines

---

## 3. Data Quality Report

```
📊 DATA QUALITY SNAPSHOT
────────────────────────────────────────────────────────────────

Ingestion Validation
├─ Rows validated: 500 ✓
├─ Schema failures: 0 ✓
├─ Checksum failures: 0 ✓
├─ Business rules failures: 0 ✓
└─ Warnings: 2 (zero-volume bars)

Research Data
├─ Market data freshness: 23 seconds old ✓
├─ Daily history freshness: 1 day old ✓
├─ Symbols missing data: 0 ✓
└─ DB integrity check: PASSED ✓

Evaluation Quality
├─ Criteria evaluated: 5 of 5 ✓
├─ Blocking reasons captured: 3 ✓
├─ Score distribution: [48%, 52%, 58%, 64%] (avg 55%)
└─ Scores < 60%: 3 symbols

Broker Connectivity
├─ API health: healthy ✓
├─ Last account sync: 2s ago ✓
├─ Last order: 45s ago ✓
└─ Latency: 342ms avg ✓
```

---

## 4. Event Timeline (Live Stream)

```
╔════════════════════════════════════════════════════════════════╗
║ EVENT TIMELINE (newest first, filter by module/symbol/type)    ║
╚════════════════════════════════════════════════════════════════╝

09:35:15.347  [evaluation]  ✓ completed    duration 1823ms, 5 evaluated, 2 ready
09:35:14.512  [evaluation]  AAPL: rvol_calculated (1.2x < 1.5x) ✗
09:35:14.381  [evaluation]  AAPL: gap_checked (2.1%) ✓
09:35:13.856  [evaluation]  TSLA: bull_flag_detected (conf 0.85) ✓
09:35:13.245  [data_validation] ingestion: schema validation PASSED (500 rows)
09:35:13.122  [research]  ✓ completed    duration 156ms, 5 researched
09:35:12.856  [ingestion]  ✓ completed    duration 245ms, 500 rows inserted
09:35:12.123  [ingestion]  ↦ started      (source: alpaca, 500 bars)
09:35:10.234  [execution]  AAPL: blocked (score 48% < 60%)
09:35:09.856  [approval_queue] → empty (all processed)
09:35:09.453  [broker]  ✓ positions synced  3 open positions
09:35:09.123  [broker]  ✓ account updated   equity $99,234
```

Click on any event to see:
- Full event JSON
- Correlation ID (trace all events for one signal)
- Copy to clipboard

---

## 5. Dry-Run Mode (Phase 7)

```bash
# Test with synthetic bull-flag setups (no API calls, no broker)
$ python run_live_paper.py --dry-run \
    --symbols AAPL,TSLA,AMD \
    --bars-provider synthetic:bull_flag \
    --num-ticks 5 \
    --inspection-dashboard

Starting dry-run mode (no API calls, no broker)
Using synthetic bull-flag bars: 50 bars/symbol
Inspection dashboard: http://127.0.0.1:8011

Tick 1 (09:30-09:35)
  Ingestion:  ✓ 150 bars collected
  Research:   ✓ 3 symbols researched
  Evaluation: ✓ 3 evaluated → 2 ready (AAPL, TSLA)
  Execution:  ✓ 2 approved
  Broker:     ✓ 2 orders submitted (simulated)

Tick 2 (09:35-09:40)
  Ingestion:  ✓ 150 bars collected
  Research:   ✓ 3 symbols researched
  Evaluation: ✓ 3 evaluated → 1 ready (AMD)
  Execution:  ✓ 1 approved
  Broker:     ✓ 1 fill (AAPL +$250), 2 pending

... (Dashboard shows all metrics live)
```

---

## 6. Module-Level Testing (Phase 6)

```bash
# Test just the evaluation module with deterministic data
$ pytest tests/modules/test_evaluation_module.py -v

tests/modules/test_evaluation_module.py::test_evaluation_ready ✓
  - Bull-flag bars pass evaluation
  - Score: 85%
  - Entry: $150.50
  - Stop: $149.00

tests/modules/test_evaluation_module.py::test_evaluation_blocked ✓
  - Fading bars fail evaluation
  - Score: 35%
  - Blocking: low volume
  - Reason: RVOL 0.8x < 1.5x

tests/modules/test_evaluation_module.py::test_evaluation_dry_run ✓
  - Can run without research DB
  - Works with injected bars
  - Full metrics returned

tests/modules/test_evaluation_module.py::test_evaluation_validation ✓
  - Invalid bars rejected
  - Validation failures captured
  - Error messages clear

tests/modules/test_evaluation_module.py::test_evaluation_contract ✓
  - Accepts EvaluationRequest
  - Returns EvaluationResult
  - All fields populated

5 passed in 0.23s
```

---

## 7. Rich TUI Mode (Phase 8, Optional)

```bash
# Real-time terminal UI (useful for SSH, CI/CD logs)
$ python run_live_paper.py --tui

╔═══════════════════════════════════════════════════════════════╗
║ MOMENTUM TRADING SYSTEM - TUI DASHBOARD                       ║
╚═══════════════════════════════════════════════════════════════╝

[Ingestion]    ████████████░░░░░░░░░░░░  50% (250/500 bars)
[Research]     ████████████████████░░░░  85% (4/5 symbols)
[Evaluation]   ████████████████████░░░░  85% (4/5 evaluated)
[Execution]    ████████████░░░░░░░░░░░░  50% (1/2 approved)
[Broker]       ███░░░░░░░░░░░░░░░░░░░░░   15% (1/5 submitted)

┌─ Module Latencies ──────────────────────────────┐
│ Ingestion: 245ms                                │
│ Research:  156ms                                │
│ Evaluation: 1823ms  ← slow (5 symbols)          │
│ Execution: 89ms                                 │
│ Broker:    342ms                                │
└─────────────────────────────────────────────────┘

┌─ Recent Events ─────────────────────────────────┐
│ [09:35:15] evaluation ✓ TSLA ready              │
│ [09:35:14] evaluation ✗ AAPL blocked (rvol)     │
│ [09:35:13] research ✓ 5 symbols researched      │
│ [09:35:12] ingestion ✓ 500 bars collected       │
└─────────────────────────────────────────────────┘

[press 'q' to quit, 's' to toggle details, 'c' to copy logs]
```

---

## Phase Completion Checklist

| Phase | What Gets Built | When You Can Use It |
|-------|-----------------|-------------------|
| ✅ 1 | Core contracts + telemetry | After Phase 1 |
| 📍 2 | Module instrumentation | After Phase 2 |
| 🔲 3 | Inspection API endpoints | After Phase 3 |
| 🔲 4 | Web dashboard frontend | After Phase 4 |
| 🔲 5 | Module refactoring | After Phase 5 |
| 🔲 6 | Module tests + fixtures | After Phase 6 |
| 🔲 7 | `--dry-run` mode + synthetic data | After Phase 7 |
| 🔲 8 | Rich TUI (optional) | After Phase 8 |

---

## Usage Scenarios

### "I want to debug why AAPL isn't signaling"

**With inspection dashboard:**
1. Open http://127.0.0.1:8011
2. Search "AAPL" in symbol inspector
3. Expand each stage to see:
   - Bars collected: 12 ✓
   - Gap: 2.1% ✓
   - RVOL: 1.2x ✗ (blocked here!)
4. Click "RVOL" stage to see:
   - Current volume: 85k
   - 20-day average: 500k
   - Ratio: 0.17x (need 1.5x)
5. Understand exactly why it's blocked

**Time saved:** 10 seconds vs. 30 minutes digging through logs

---

### "I want to test the evaluation module alone"

**With Phase 6:**
```bash
pytest tests/modules/test_evaluation_module.py -v
```

- No database needed
- No API calls
- Deterministic test data (bull flags, fading bars)
- See exactly what each criterion does
- Change thresholds and re-test instantly

---

### "I want to see if my strategy works with dry-run data"

**With Phase 7:**
```bash
python run_live_paper.py --dry-run \
  --bars-provider synthetic:bull_flag \
  --num-ticks 5
```

- No API keys needed
- No broker interaction
- Dashboard still works
- See signals, approval, execution all flow through
- Test configuration changes risk-free

---

## Summary

**Before Phases 1-4:** Black box ("did it work?")

**After Phases 1-4:** Transparent pipeline ("here's exactly what happened at each step, why it passed/failed, and how long it took")

This is the foundation for:
- ✅ Debugging blocked signals in seconds
- ✅ Testing modules independently
- ✅ Verifying data accuracy at each stage
- ✅ Dry-running strategies without real APIs
- ✅ Understanding your system's behavior

Next: **Phase 2** instruments the existing modules. Then Phases 3-4 build the visual dashboard to make all this data human-readable.
