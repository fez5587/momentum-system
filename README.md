# Momentum

Event-sourced small-cap momentum day-trading system (Ross Cameron–style):
gap-and-go / bull-flag setups on 1-minute bars, with paper execution on
Alpaca and a live-broker integration for Schwab.

> **Important rebuild note.** This codebase was reconstructed from scratch
> against the original architecture and interfaces after the original source
> files became unrecoverable in the working environment. Diff it against your
> local copy before discarding anything — module layout and public interfaces
> were preserved, but implementations are fresh. See `RUNBOOK.md` for what
> changed and why.

## What this system does

```
Alpaca IEX bars ──► research/market.duckdb ──► Watcher ──► signal_ready
   (1-min, live)        (minute + daily)          │
                                                  ▼
Dashboard (approve/reject) ◄── approval queue ◄── TradingExecutionService
        │                                          │
        ▼                                          ▼
   exit orders ─────────────────────────► Alpaca paper account
                                                  │
                            AlpacaPaperSync ◄─────┘
                       (account/positions/orders → event store → dashboard)
```

Everything observable flows through a single append-only **event store**
(`data/momentum.duckdb`): symbol discovery, criteria scores, signals,
approval requests, orders, fills, account snapshots, broker health. The
dashboard and all read paths are pure projections over those events, so any
session can be replayed and audited.

## Milestones

| # | Scope | Status |
|---|-------|--------|
| M1 | Strategy logic — setup evaluation, structure detection, quality, backtest | ✅ `strategy/` |
| M2 | Event store + projections | ✅ `storage/` |
| M3 | Schwab integration — OAuth, market data, positions/orders, health | ✅ `schwab/` (`python milestone3_verify.py`) |
| M4 | Watcher + live paper execution | ✅ `runtime/`, `alpaca_paper/`, `trading_execution.py`, `run_live_paper.py` |
| M5 | Dashboard UI + API | ✅ `api/`, `dashboard_api.py` |

## Quick start (paper trading)

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in ALPACA_API_KEY / ALPACA_SECRET_KEY

python run_live_paper.py    # full loop + dashboard at http://127.0.0.1:8010
```

Signals appear in the dashboard's **Approval Queue**; click **Approve** to
send the bracket order to your Alpaca paper account (or start with
`--auto-approve` / `TRADING_AUTO_APPROVE=1`). Use the **Exit** button on a
position to close it at market.

No keys? `python run_live_paper.py --once` still runs in dry mode so you can
verify wiring.

### Dashboard (control room)

The dashboard is a single self-contained page (no build step) that streams
live state over Server-Sent Events and falls back to polling if the stream
drops. It shows:

- **P&L strip** — realized, unrealized, total, win rate, average R, and
  open/closed trade counts for the session.
- **Live sparkline charts** on every ready signal and open position, drawn
  from 1-minute bars with dashed entry (blue) and stop (red) reference lines.
- **Pre-trade risk preview** in the approval queue — dollar risk, share count,
  notional, and stop distance before you approve.
- **Click-to-expand criteria** on any watched symbol — all nine setup criteria
  as pass / fail / not-evaluated, so a "blocked" symbol is legible at a glance.
- **Activity feed** of submissions and fills, plus the full order lifecycle.
- **Audio + toast alerts** when a new signal goes ready (toggle the sound
  button, or press `S`).
- **Manual symbol injection** — type a ticker (or press `/`) to force it onto
  the watchlist mid-session.
- **Keyboard shortcuts** — `A` approve / `R` reject the top of the queue,
  `S` toggle sound, `/` focus the add-symbol box, `?` help.

## Repository map

```
strategy/            pure strategy engine: evaluation, structure, backtest
storage/             event schema, event store, DuckDB schema, projections
runtime/             watcher state machine + provider registry
research/            research DBs, ingestion (bars, RSS, gappers), providers
alpaca_paper/        Alpaca paper client, executor, account sync
schwab/              Schwab OAuth, market data, positions/orders, health
trading_execution.py signal → sizing → approval → order pipeline
run_live_paper.py    the orchestrator (M4 end-to-end)
api/                 dashboard JSON API + static UI
dashboard_api.py     standalone read-only dashboard server
tests/               unit + integration suite (pytest)
```

CLI utilities: `fetch_minute_bars.py` (ad-hoc bar ingestion),
`backtest_cli.py` (intraday backtests over stored sessions),
`research_cli.py` (session symbols / gapper scan / RSS news),
`milestone3_verify.py` (Schwab health walk-through).

## Tests

```bash
python -m pytest          # 60 tests: strategy, storage, watcher, execution,
                          # schwab, api, research, end-to-end pipeline
```

## Security

`data/schwab_tokens.json` (and all of `data/`) is git-ignored and written
with `0600` permissions. **If tokens were ever committed to your repository
history, revoke and re-issue them at Schwab** — see RUNBOOK.md.
