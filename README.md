# Momentum

Event-sourced small-cap **momentum day-trading** system (Ross Cameron–style
gap-and-go / opening-range breakout). It screens the day's most-active gappers
($1–5, gapping ≥3% on ≥2× relative volume), arms an opening-range-breakout
trigger the moment the range forms, fires a bracket order on the live cross,
and manages the exit with a trailing stop + profit-lock ladder. Execution runs
on an **Alpaca paper** account; everything is recorded in a **PostgreSQL**
append-only event store and replayable.

```
Alpaca IEX bars ─► research DBs (minute+daily) ─► screen gappers ─► ArmedTriggerBook
   (1-min, live)                                                          │ live price
                                                                          ▼ crosses trigger
   PostgreSQL event store  ◄──────────────────────  TradingExecutionService ─► Alpaca paper
   (append-only: signals, orders,                            │ bracket (entry+stop+target)
    fills, account, risk events)                            ▼
        ▲                                          LiveExitManager ─► trail stop ↑ / flatten
        └──────────── AlpacaPaperSync ◄──────── (account / positions / orders)
```

Every read path (dashboard, journal, monitoring board) is a pure **projection**
over the event store, so any session can be replayed and audited.

## Quick start (paper trading)

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in ALPACA_API_KEY / ALPACA_SECRET_KEY + DATABASE_URL

# PostgreSQL is the datastore. Point DATABASE_URL at your instance, e.g.
#   DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/momentum

python run_live_paper.py      # full loop (+ embedded dashboard)
```

No keys? `python run_live_paper.py --once` runs a single dry pass to verify wiring.

## Commands

Everything is driven from the repo root. The `./momentum` wrapper runs the
inspection CLI in the project venv; the analysis scripts are plain `python`.

### Live trading loop — `run_live_paper.py`

| Command | What it does |
|---------|--------------|
| `python run_live_paper.py` | Full live loop: screen → arm → fire → manage exits → EOD flatten, with the embedded dashboard. |
| `python run_live_paper.py --no-dashboard` | Same, headless (how it runs in production / under a process manager). |
| `python run_live_paper.py --once` | One pass then exit — smoke-test the wiring. |
| `python run_live_paper.py --auto-approve` | Auto-arm brackets instead of waiting for dashboard approval (`TRADING_AUTO_APPROVE=1`). |
| `python run_live_paper.py --symbols AAPL,SNDL` | Force specific symbols onto the watchlist. |

### Monitoring & inspection — `./momentum`

| Command | What it does |
|---------|--------------|
| `./momentum watch` | Live monitoring board (armed triggers, ready signals, **today's trades + day P&L**, risk events). Refreshes every 3s. |
| `./momentum watch --once` | Render one frame and exit (for logging / a cron snapshot). |
| `./momentum journal` | Today's trade journal — every round-trip (ET times), broker-authoritative day P&L (`matched · open`), win rate. The 16:02 close capture. |
| `./momentum doctor` | Health check across each pipeline stage (DB reachable, ingest fresh, broker sync, loop heartbeat). |
| `./momentum inspect events` | Tail the raw event stream. |
| `./momentum inspect bars SYM` | Today's minute bars for a symbol. |
| `./momentum inspect discovery` | What the screener surfaced. |
| `./momentum inspect criteria SYM` | Per-criterion pass/fail for one symbol (why it is/isn't a setup). |
| `./momentum inspect signals` | Recent ready signals. |

### Analysis, backtest & tuning

| Command | What it does |
|---------|--------------|
| `python eod_replay.py [YYYY-MM-DD]` | End-of-day whole-market replay: run **today's logic** over every qualifying gapper (not just the ~20 watched live) + a perfect-hindsight line. Also **grows the stored dataset** for future verification. Defaults to today. |
| `python backtest_cli.py` | Intraday backtests over stored sessions. |
| `python sweep_exits.py` | Sweep managed-exit variants (trail / breakeven / profit-lock tiers), train/test split. |
| `python sweep_filters.py` | Sweep entry-filter variants (gap / rvol / price / liquidity). |
| `python diagnose_entries.py` | Entry-quality analysis — what the losers had in common. |
| `python nightly_tune.py` | Self-tune params into `data/learned_params.json` (applied on next boot). |
| `python research_cli.py` | Ad-hoc: session symbols, gapper scan, RSS news pull. |
| `python milestone3_verify.py` | Schwab integration health walk-through. |

### Data ingestion

| Command | What it does |
|---------|--------------|
| `python fetch_minute_bars.py` | Ad-hoc minute-bar ingestion. |
| `python backfill_history.py` | Backfill daily/minute history for the universe. |

### Scheduled jobs (cron wrappers)

The `.sh` wrappers set the venv + cwd so cron can call them directly:

```cron
2  16 * * 1-5  /path/momentum/journal.sh       # capture the close trade journal
5  16 * * 1-5  /path/momentum/eod_replay.sh     # whole-market replay (grows dataset)
30 17 * * 1-5  /path/momentum/nightly_tune.sh   # retune params for tomorrow
```

### Tests

```bash
DATABASE_URL=postgresql://user:pass@host:5432/momentum python -m pytest
# ~130 tests: strategy, storage, triggers, execution (cooldown/re-entry/EOD
# flatten/naked-stop guards), exits, journal P&L, schwab, api, research.
```

Tests isolate via throwaway `mem_*` Postgres schemas; `tests/conftest.py` sweeps
them at session end.

## Configuration

All behavior is environment-driven — see **`.env.example`** for every key with
the *why*. The knobs that matter most:

- **Entry:** `WATCHER_PRICE_MIN/MAX` (1–5), `TRIGGER_GAP_MIN/MAX`, `TRIGGER_RVOL_MIN`,
  `TRIGGER_MIN_DOLLAR_VOL` (absolute liquidity floor — skip thin spikes).
- **Sizing:** `TRADING_RISK_PER_TRADE_PCT` (1%), `TRADING_MAX_POSITION_PCT`,
  `TRADING_LIQUIDITY_MAX_VOLUME_PCT` (size down thin names).
- **Exits:** `TRADING_EXIT_TRAIL_MODE=prior_low`, `TRADING_EXIT_TRAIL_AFTER_R`,
  `TRADING_EXIT_BREAKEVEN_R`, `TRADING_EXIT_PROFIT_TIERS`.
- **Discipline:** `TRADING_BACKOUT_COOLDOWN_SECONDS`, `TRADING_BLOCK_REENTRY_AFTER_EXIT`
  + `TRADING_REENTRY_MIN_LOSS_PCT`, `TRADING_EOD_FLATTEN_TIME`.
- **Safety:** `TRADING_MAX_DAILY_LOSS_PCT` — **set to `0.03` before real-money
  trading** (it ships loosened for testing).

## Database & performance

The single datastore is **PostgreSQL** (`DATABASE_URL`); research bars live in
DuckDB under `data/research/`. The event store is one append-only `events`
table that every projection reads.

**Hot-path index.** Projections overwhelmingly run
`WHERE event_type=? ORDER BY timestamp DESC LIMIT n` (latest snapshot of a type).
A composite index makes this an index seek regardless of how stale the type is —
without it, looking up the latest of an idle type backward-scans the whole table
(measured **373 ms → 0.018 ms** on 235k rows). It's created automatically by
`storage/event_store.py`:

```sql
CREATE INDEX idx_events_type_ts ON events (event_type, timestamp DESC);
```

**Plugins / extensions.** The big win here is the index above, not an extension.
For ongoing tuning:

- **`pg_stat_statements`** — enable (`shared_preload_libraries`, server restart)
  to see which queries actually cost time. Diagnostic, not a speedup itself.
- **`pg_prewarm`** — warm the cache for the `events` table after a restart.
- **TimescaleDB** — only worth it if the event/bar volume grows large (hypertables
  + time partitioning + compression). Not required at current scale, and must be
  installed on the server.

**Maintenance.** ~80% of `events` rows are high-volume diagnostics
(`criteria_evaluated`, `signal_blocked`, `module_tick`). A retention/prune of old
diagnostic events keeps the table small and every query fast. Run `ANALYZE events`
after large ingests so the planner has fresh stats.

## Repository map

```
run_live_paper.py     the orchestrator: screen → arm → fire → manage → EOD flatten
momentum_cli.py       the `momentum` CLI (watch / journal / doctor / inspect)
trading_execution.py  signal → sizing → approval → bracket order; risk discipline
runtime/              triggers (ArmedTriggerBook), exit_manager, flatten helper
strategy/             pure strategy engine: evaluation, structure, exits, backtest
storage/              event schema + store (PostgreSQL), projections
research/             research DBs, ingestion (bars, RSS, gappers), screeners
alpaca_paper/         Alpaca paper client, executor, account/order sync
schwab/               Schwab OAuth, market data, positions/orders, health
api/ + dashboard_api.py   dashboard JSON API + static UI
eod_replay.py         end-of-day whole-market replay
tests/                pytest suite (unit + integration)
```

## Security

`.env` and all of `data/` (DBs, `data/schwab_tokens.json`) are git-ignored and
never committed; token files are written `0600`. The `.env` holds **live broker /
database / webhook credentials** — keep it local. If any secret was ever
committed to a repo's history, **rotate it** (re-issue Alpaca keys / re-run the
Schwab OAuth flow).
