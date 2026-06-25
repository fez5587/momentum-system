# Momentum-ignition setup — shadow scope

_Scope produced 2026-06-24 by a read-only 4-agent mapping workflow (wf_55582b6d). No code changed.
A SECOND, shadow-only lane to catch PLSM-class vertical squeezes the pullback-ORB scorer is blind to._

## Why
The live setup is `impulse → pullback → breakout` (`strategy/evaluation/structure.py` `classify_setup`).
A true vertical squeeze gives **no pullback and no formed opening range**, so `classify_setup` returns
NONE and the scorer blocks it — verified: PLSM was evaluated **1,895× on 2026-06-24 and blocked ~30%
every time**. This lane is the missing discipline: ride the vertical, not the base.

## What it detects
An intraday vertical ignition with no consolidation: a fresh **high-of-day above the prior all-time
high (blue sky)**, **accelerating price velocity** (consecutive green bars + an X% move over K minutes),
and an **absolute volume burst**. It deliberately catches the pattern that makes `classify_setup`
return NONE.

## Detection rules — all computable TODAY (binary, no fitting at this n)
| signal | rule | source |
|---|---|---|
| **Blue-sky fresh-HOD** | `current.high > prior_ATH` (max `daily_bars.high` before today) AND this bar is the running session-high HOD. *Don't* reuse `detect_hod_break` (needs consolidation a vertical never gives). | `daily_bars.high` + `minute_bars.high` |
| **Price velocity** | `pct_move(close[t]/close[t-N]-1) ≥ 0.08` over N=5–10 RTH min, AND ≥4 of last 5 bars green, AND higher-highs. (PLSM 12:02–12:16: 10/14 green, +13.9% in 14 min.) | `minute_bars` OHLC, pure arithmetic |
| **Absolute volume burst** | `last_bar_vol / mean(prior K=5 bars) ≥ 3.0` AND cumulative session volume ≥ 100k shares (or a $-volume floor). **Absolute floors, NOT RVOL** — `scan_gappers` RVOL zeros out on thin IPOs (adv=0), and `rolling_avg_volume_20d` is NULL. | `minute_bars.volume` |
| catalyst *(tag only)* | attach max-conviction bullish row (`sentiment>0`, `is_dilutive=False`); never a gate (Ollama gated off / can be down). PLSM "Soaring" → 0.9 conviction. | `news_catalyst_cache` |
| VWAP *(tag only)* | log `above_vwap` + `dist_from_vwap` as grading context (the one validated ~1.5× signal). | `compute_key_levels.vwap` |
| float *(placeholder)* | **DO NOT GATE** — `symbols` table is empty, `float_shares` NULL. Log `None` so a later float backfill can re-grade historical shadows. | `symbols.float_shares` (empty) |

## Hook point
A new zero-arg closure **`step_shadow_ignition()`** in `run_live_paper.py` beside `step_watch` (~L528),
registered via `scheduler.add('shadow_ignition', …, enabled=_flag('SHADOW_IGNITION_ENABLED','0'))` —
**ships dark**. Pure-read against `research_con` (single psycopg2 conn — not thread-safe), emit-only,
never references `rt['execution']`. Reuses the watcher provider's `get_candidates`/`get_bars` (same
symbols/bars) but with its OWN wider caps (`SHADOW_PRICE_MAX≈100`, no gap ceiling) so it validates the
very names live config excludes. Detection is a new pure `detect_momentum_ignition(bars)` in
`strategy/evaluation/ignition.py` — **NOT** wired into `classify_setup`/`evaluate_setup` (the pullback
criteria would reject it and `STRATEGY_SETUPS=opening_range_break` would filter it).

## Shadow mechanism — structurally cannot trade
New `EventType.SHADOW_SIGNAL` + `ShadowSignalEvent` (symbol, trigger, would-be stop, signal_values
dict, session_date). It is **invisible to execution**: `query_ready_signals_snapshot` filters strictly
on `event_type='signal_ready'`, the only thing `TradingExecutionService.tick` reads — a `shadow_signal`
is never seen, and the lane never calls `submit_breakout_now`/`request_approvals`. (Precedent: the VWAP
gate already shadow-logs via `RiskRuleTriggeredEvent action_taken=shadow_logged`.) Safety: wrap the
step in try/except (a shadow failure can't kill the loop), once-per-session per-symbol debounce (so one
squeeze logs once, not 1,895×), never emit `signal_ready`, never set a symbol "ready".

## Measurement — reuse existing forward-outcome machinery (zero new metric code)
1. **Labeler**: add `build_ignition` to `research/labeler.py` (`setup_name=momentum_ignition`,
   `setup_version=ign_v1`) reusing the identical label block (`reached_1r/2r_before_minus_1r`,
   `max_upside_5/15/60m`, `time_to_max`, `failed_breakout_flag`=the fizzle trap, `held_vwap`). `report`
   /`lift` filtered by version give runner-vs-trap separation per signal.
2. **eod_replay**: add `score_shadow_signals` — after the close, read the day's actual `shadow_signal`
   events, walk `minute_bars` strictly after each, classify **runner (+1R) / fizzle / halt-down-trap**
   (the FCUV mode: a forward bar gapping far below the prior low, or R ≤ −1 within minutes). Run nightly.

## Promotion bar — PRE-REGISTER before shadowing (so the bar can't move to fit noise)
- `reached_1r` rate **materially above** the `failed_breakout` trap rate, AND
- halt-down-trap rate **below a ceiling**, AND
- **distinct runner instances** above the thin-n floor (≥30, or rate×count ≥8) — counted in distinct
  runners, NOT calendar days (the ~17–19 days are autocorrelated, one regime).
- Decision hangs on the **eod_replay as-fired** numbers (what a live lever would actually capture).

## Build phases
| phase | blast | what |
|---|---|---|
| **P0** | none | `detect_momentum_ignition(bars)` pure + unit-tested (synthetic bars + the PLSM 12:02–12:16 window). No wiring. |
| **P1** | low | `SHADOW_SIGNAL`/`ShadowSignalEvent` + `step_shadow_ignition` (default OFF, own caps, debounce, try/except). Test asserts it NEVER emits `signal_ready` / never calls execution. |
| **P2** | low | `build_ignition` (labeler) + `score_shadow_signals` (eod_replay), nightly. DB-unreachable tolerant; tests use `:memory:`/`MOMENTUM_PG_SCHEMA`. |
| **P3** | none | Pre-register the promotion thresholds; shadow N weeks; accumulate. Zero capital, zero trading-path edits. |
| **P4** | high | *Only after P3 separation is proven* — a tiny live lever behind `require_ignition_confirmed` (default OFF, mirrors `require_above_vwap`), minimally sized, bounded by ALL shipped catastrophe controls (enforced stops, anti-chase, BE@+5%, halt guard). The ORB book + anti-chase guards untouched — strictly additive. |

## Open questions for you
- **Stop definition** for the R denominator: last-higher-low vs bar-low vs −8%? Changes every reached_1r/trap rate — pick ONE and pre-register. (Maps lean last-higher-low.)
- **Shadow gap ceiling**: drop entirely or cap ~80%? Dropping surfaces the most extreme/trap-prone names (fine for shadow).
- **Catalyst tag** needs Ollama running that day — force-enable it for the shadow grading pass so news-backed vs not can be measured?
- **Distinct-runner-n ≥30** may be unrealistic given runner sparsity — a Bayesian interval at low n instead of a point rate?

## Hard constraints (non-negotiable)
Shadow-only until forward data proves it · never touches the ORB book or anti-chase guards · ships dark
(`SHADOW_IGNITION_ENABLED=0`) · binary rules, no fitting · do NOT gate on RVOL or float (both broken for
exactly these names) · tests use `:memory:`/`MOMENTUM_PG_SCHEMA` (never the live DB).
