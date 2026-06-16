# RUNBOOK

Operational guide for running the momentum system. This file replaces the
original MILESTONE*.md documents, which were unrecoverable during the
rebuild.

---

## 1. Why live paper trading never worked (root causes, now fixed)

1. **Minute bars were never ingested.** The watcher reads 1-minute bars from
   `data/research/market.duckdb`, but the old ingestion only wrote *daily*
   bars for a handful of mega-caps. Fix: `research/ingestion/market_data.py`
   ingests 1-minute Alpaca IEX bars continuously, and
   `run_live_paper.py` schedules it every `LIVE_BARS_INTERVAL_SECONDS`.

2. **Session flags were hardcoded.** Every old row had
   `is_regular_hours = TRUE`. Fix: flags are derived from the bar timestamp
   in US/Eastern (premarket 04:00–09:30, regular 09:30–16:00, afterhours
   16:00–20:00) — see `classify_session()` and its tests.

3. **The candidate filter excluded everything ingested.** The query had a
   hardcoded `close BETWEEN 1 AND 20` while only mega-caps (price ≫ 20) were
   in the database, so the watchlist was always empty. Fix: the band is
   configurable (`WATCHER_PRICE_MIN` / `WATCHER_PRICE_MAX`) and the default
   symbol list now actually gets ingested.

4. **No orchestrator.** Ingestion, watcher, broker sync, and execution were
   never wired into one loop. Fix: `run_live_paper.py`.

5. **Broken source files.** `runtime/providers/registry.py`,
   `schwab/market/models.py`, `option_chain_service.py`, and
   `schwab/streaming/client.py` had syntax errors that crashed imports.
   All repaired/rewritten.

6. **Schwab tokens were committed to git.** `data/schwab_tokens.json` is now
   git-ignored and written `chmod 0600`. **Action required:** if that file
   exists anywhere in your repo history, revoke the refresh token in the
   Schwab developer portal and re-run the OAuth flow. Scrub history with
   `git filter-repo --path data/schwab_tokens.json --invert-paths` if the
   repo was ever shared.

---

## 2. Environment setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Minimum for paper trading: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`
(free keys are fine — the system uses the IEX feed). Everything else has a
sensible default; see `.env.example` for the full catalog.

Key knobs:

| Variable | Default | Meaning |
|---|---|---|
| `TRADING_EXECUTION_MODE` | `alpaca_paper` | `schwab_live` routes to real money — leave on paper |
| `TRADING_AUTO_APPROVE` | `0` | `1` = orders submit without dashboard approval (or pass `--auto-approve`) |
| `TRADING_MAX_ORDERS_PER_TICK` | `1` | throttle new entries |
| `TRADING_MAX_CONCURRENT_POSITIONS` | `3` | cap on simultaneous open positions |
| `TRADING_RISK_PER_TRADE_PCT` | `0.01` | 1% of equity risked per trade (sizing = risk / (entry − stop)) |
| `TRADING_REWARD_MULTIPLE` | `2.0` | profit target = entry + this × risk (2R by default) |
| `TRADING_ENTRY_ORDER_TYPE` | `limit` | `limit` rests at the trigger so an unfilled entry can be cancelled; `market` fills instantly |
| `TRADING_ENTRY_TIMEOUT_BARS` | `2` | cancel an unfilled entry after this many minutes resting (wall-clock; `0` disables the time box) |
| `TRADING_ENTRY_INVALIDATE_PCT` | `0.0` | cancel an unfilled entry if the **live last-trade price** trades this fraction below the trigger (`0.0` = any break below entry; `0.005` = 0.5% below; negative disables) |
| `TRADING_ENTRY_GUARD_INTERVAL_SECONDS` | `5` | how often the fast guard re-checks armed entries against the live price — lower = faster break detection, more quote calls |
| `TRADING_MAX_DAILY_LOSS_PCT` | `0.03` | daily-loss circuit breaker |
| `WATCHER_SYMBOLS` | `AAPL,TSLA,…` | static watchlist; `--discover` adds screener actives |
| `WATCHER_PRICE_MIN/MAX` | `1` / `20` | candidate price band |
| `WATCHER_EVENT_DB_PATH` | `./data/momentum.duckdb` | the event store |
| `DASHBOARD_HOST/PORT` | `127.0.0.1` / `8010` | UI address |

---

## 3. Start commands

**Full live paper loop (recommended):**

```bash
python run_live_paper.py
# dashboard:  http://127.0.0.1:8010
# flags:      --once  --symbols SNDL,COSM  --auto-approve  --discover  --no-dashboard
```

Each tick prints one status line:

```
[09:31:02] ingest: 240 minute rows | watch: evaluated=5 ready=['SNDL'] blocked=3 | sync: ok | execute: approvals_requested=['…'] auto_executed=0
```

**Standalone read-only dashboard** (e.g. to review a past session):

```bash
python dashboard_api.py
```

Approve/reject/exit buttons require the orchestrator (it attaches the
execution service to the same server); the standalone runner returns 503 on
actions by design.

**Other tools:**

```bash
python fetch_minute_bars.py SNDL COSM --lookback 390   # ad-hoc ingestion
python research_cli.py symbols --date 2026-06-11       # what's in the DB
python research_cli.py gappers --date 2026-06-11       # gap scan → scanner_snapshots
python backtest_cli.py --date 2026-06-10               # backtest a stored session
python milestone3_verify.py                            # Schwab health walk
python -m pytest                                       # full test suite
```

---

## 4. The trading day, end to end

1. **Ingest** — every 60 s, 1-minute IEX bars for the watchlist land in
   `data/research/market.duckdb` with correct session flags. On boot, a
   30-day daily backfill gives gap % and relative volume a baseline.
2. **Watch** — every 30 s the watcher pulls in-band candidates, evaluates
   nine weighted criteria (gap, RVOL, impulse, pullback, pullback volume,
   VWAP, candle quality, breakout, data sufficiency) plus structure
   detection, and drives `discovered → watching → ready | blocked | late`.
   A `signal_ready` is emitted at most once per symbol per session.
3. **Sync** — Alpaca account, positions, and orders snapshot into the event
   store so projections and risk guards see broker truth.
4. **Execute** — ready signals become orders sized at `risk_per_trade_pct`
   of equity, with the stop from the setup and the target at
   entry + `reward_multiple` × risk (2R by default). Manual mode parks them
   in the dashboard queue; **auto mode (`--auto-approve`) submits
   immediately, no approval.** Guards: max concurrent positions, no re-entry
   into held symbols, one request per symbol per session.
5. **Manage** — approve/reject from the dashboard; **Exit** sends a market
   sell for the synced quantity.

### The entry lifecycle (auto-arm → fill or back out)

The earlier system would "see" a setup as something that had *already
happened* rather than a trade about to happen, and never pull the trigger.
The execution model now separates recognition from conviction from
discipline:

1. **Recognize** — when all the criteria pass, a `signal_ready` fires. The
   setup is evaluated against the **last bar's timestamp**, not the wall
   clock, so a live 9:40 breakout is judged against the 9:40 cutoff instead
   of being dismissed as hours stale.
2. **Arm with conviction** — in auto mode the order is submitted right away
   as a **bracket** (entry + stop + target placed atomically). With
   `TRADING_ENTRY_ORDER_TYPE=limit` (default) the entry rests at the trigger,
   so "unfilled" is a real, observable state.
3. **Back out at a defined point** — a fast **guard loop**
   (`TRADING_ENTRY_GUARD_INTERVAL_SECONDS`, default 5 s) re-checks every
   armed-but-unfilled entry and cancels it if either:
   - the **live last-trade price** (pulled tick-by-tick from Alpaca's data
     API, not a bar close) trades back below the trigger by
     `TRADING_ENTRY_INVALIDATE_PCT`, or
   - it has rested longer than `TRADING_ENTRY_TIMEOUT_BARS` minutes
     (wall-clock — running the guard often does **not** shorten this window).
   A cancel frees the risk budget and the concurrent-position slot, and emits
   a `risk_rule_triggered` (`entry_backout`) you can see in the activity feed.
   Once the order **fills**, it stops being tracked and the bracket's stop and
   target manage the open position — no second-guessing.

Tuning cheat-sheet: react to a fakeout faster → lower
`TRADING_ENTRY_GUARD_INTERVAL_SECONDS` (e.g. `2`); more patience on the fill →
raise `TRADING_ENTRY_TIMEOUT_BARS`; give the breakout room to wobble → set
`TRADING_ENTRY_INVALIDATE_PCT` to something like `0.005`; chase fills instead
of resting → `TRADING_ENTRY_ORDER_TYPE=market` (note: market fills can't time
out); bigger winners → raise `TRADING_REWARD_MULTIPLE`.

> **Quote rate limits.** The guard makes one latest-trade call per armed
> symbol per pass (a ~2 s cache collapses duplicates). At the 5 s default with
> a few open entries you're well inside Alpaca's free-tier 200 req/min. If you
> drop the interval to 1 s with several armed entries, watch the budget —
> bars ingestion and account sync also draw from it.

### Reading the dashboard

- **Badge** — blue `PAPER · ALPACA` vs red `LIVE · SCHWAB`. If it's red and
  you didn't mean it, stop and check `TRADING_EXECUTION_MODE`.
- **P&L strip** (top) — session realized / unrealized / total, win rate,
  average R, open/closed counts. Green/red signed.
- **Approval Queue** — pending entries with a **risk preview** ($ risk,
  shares, notional, stop %) and Approve/Reject. Keyboard: `A` / `R` act on the
  top row.
- **Ready Signals / Open Positions** — each row has a live **sparkline** (last
  ~40 one-minute closes) with dashed entry (blue) and stop (red) lines.
- **Watch States** — click any row to expand the **nine-criteria breakdown**
  (✓ pass / ✕ fail / · not-evaluated). The add-symbol box (or `/`) injects a
  ticker onto the watchlist for the rest of the session.
- **Activity Feed** — submissions and fills as they happen.
- **Order Lifecycle** — per-order status from the event store.
- **Connection dot** (top right) — green pulse = live SSE stream; it falls
  back to 4 s polling automatically if the stream drops.
- **Sound** — toggle the button or press `S` for an audio + toast alert when a
  new signal goes ready. Press `?` for the shortcut list.

Streams from `GET /api/stream` (SSE); also exposes `/api/snapshots`,
`/api/criteria?symbol=`, `/api/bars?symbol=&minutes=`, and
`POST /api/watch/add`. Charts require the research DB, which the orchestrator
wires in automatically; the standalone `dashboard_api.py` has no bars source,
so sparklines there show "no bars".

---

## 5. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `watch: evaluated=0` all day | No minute bars for today. Check keys; run `python research_cli.py symbols`. Outside market hours there are no new bars — backfill with `fetch_minute_bars.py --lookback 1440`. |
| Candidates exist but never `ready` | Score < 60%. Watch the `Watch States` panel score column; criteria details are in `criteria_evaluated` events. |
| `ingest: … 403` errors | Free keys are IEX-only — ensure `ALPACA_DATA_FEED=iex` (default) and that the request window ends ≥30 s in the past (handled automatically). |
| Approve button does nothing | You're on `dashboard_api.py` (read-only). Use `run_live_paper.py`. |
| Port 8010 busy | Set `DASHBOARD_PORT`, or `--no-dashboard`. |
| Schwab shows `SCHWAB-UNAUTH` | Expected without OAuth. Run the flow in `schwab/auth/oauth2_flow_manager.py`; verify with `milestone3_verify.py`. |
| Start fresh | Delete `data/momentum.duckdb` (events) and/or `data/research/` (bars). Both rebuild automatically. |

---

## 6. Going live on Schwab (when ready)

Paper-trade until the stats earn it. Then: complete OAuth
(`milestone3_verify.py` all green), set `TRADING_EXECUTION_MODE=schwab_live`,
keep `TRADING_AUTO_APPROVE=0`, and start with the smallest viable
`TRADING_RISK_PER_TRADE_PCT`. The dashboard badge turns red in live mode.
