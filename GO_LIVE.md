# Go Live — market-open runbook

The system is verified end-to-end on real data (Postgres backend, sub-$20
discovery, live signals in the morning window). This is how to run and monitor
it at the open.

## 0. Preconditions (already done)
- Postgres reachable at `127.0.0.1:5432` (your LAN instance via the port proxy).
- `.env` has `DATABASE_URL=postgresql://admin:password@127.0.0.1:5432/momentum`
  and valid `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`.
- Linux venv at `/home/philip/.venvs/momentum`.

## 1. Start it (LIVE PAPER TRADING — enabled)
```bash
cd /mnt/c/code/momentum
PYTHONPATH=. /home/philip/.venvs/momentum/bin/python run_live_paper.py --no-dashboard
```
Each loop: **discover** sub-$20 most-actives, ETF-filtered (every 5 min) →
**ingest** 1-min bars (every 60s) → **watch**/evaluate (every 30s) → on a ready
signal, **auto-arm a bracket order** to your Alpaca paper account → **sync**
account/positions → **guard** unfilled entries. Stop with **Ctrl-C** (graceful).

Guardrails (`.env`): 1% risk/trade, max 3 concurrent, 2R targets, and a
**-3% daily-loss circuit breaker** that halts new entries. Trading window
**9:30–10:35 ET**.

> Start it a few minutes before 9:30 ET so the premarket screen + daily backfill
> are warm at the open.

## 2. The trading window
Signals fire **9:30–10:35 ET**. The entry cutoff is **10:30 ET** (+5 min grace,
Ross-Cameron style) — after that, qualifying setups are marked **late**, not
ready. So watch the first hour.

## 3. Monitor (any terminal, or remotely)
```bash
M="PYTHONPATH=. /home/philip/.venvs/momentum/bin/python momentum_cli.py"
$M doctor                 # PASS/WARN/FAIL across every stage
$M inspect signals        # ready board + per-symbol gap/rvol/status (the main view)
$M inspect bars           # per-symbol bar counts + freshness (are bars flowing?)
$M inspect criteria RGNT  # full per-criterion reasons for one symbol
$M inspect discovery      # current sub-$20 universe + ranked gappers
$M inspect events --limit 30
```
Or point **pgAdmin** at the `momentum` DB: `events`, `daily_bars`,
`minute_bars`, `scanner_snapshots` are all live.

## 4. Trading is ENABLED — how to dial it back
`.env` already has live paper trading on, with guardrails:
```
TRADING_EXECUTION_ENABLED=1
ALPACA_PAPER_SYNC_ENABLED=1
TRADING_AUTO_APPROVE=1            # auto-arm bracket orders (hands-off)
TRADING_MAX_DAILY_LOSS_PCT=0.03  # -3% daily-loss circuit breaker (implemented + verified)
TRADING_RISK_PER_TRADE_PCT=0.01  # 1% of equity per trade
TRADING_MAX_CONCURRENT_POSITIONS=3
```
- **Manual approval instead of auto:** `TRADING_AUTO_APPROVE=0`, drop
  `--no-dashboard`, approve each signal at `http://127.0.0.1:8765`.
- **Observe only (no orders):** `TRADING_EXECUTION_ENABLED=0`.
- The breaker at -3% halts **new** entries **and flattens the book** — cancels
  unfilled entries + market-closes open positions. Set
  `TRADING_FLATTEN_ON_BREACH=0` for halt-only (leave positions open).

## 5. Tuning (env vars)
| var | default | meaning |
|-----|---------|---------|
| `WATCHER_PRICE_MIN` / `MAX` | 1 / 20 | discovery + watcher price band |
| `DISCOVER_TOP` | 20 | screener universe size |
| `DISCOVER_INTERVAL_SECONDS` | 300 | how often to re-screen |
| `WATCHER_INTERVAL_SECONDS` | 30 | evaluation cadence |
| `LIVE_BARS_INTERVAL_SECONDS` | 60 | minute-bar ingest cadence |
| `WATCHER_MIN_BARS` | 10 | min bars before evaluating |

## 6. Reset to a clean slate (optional)
```bash
# clears event stream + scans; KEEPS the daily-bar gap baseline
/home/philip/.venvs/momentum/bin/python - <<'PY'
import psycopg2
c=psycopg2.connect("postgresql://admin:password@127.0.0.1:5432/momentum"); c.autocommit=True
c.cursor().execute("TRUNCATE events, scanner_snapshots")
PY
```

## Known limitations (honest)
- `quality_score` is a constant 1.0, `float_rotation` is 0.0, `spread_pct` is
  null — these fields are not real metrics yet.
- News-catalyst discovery (RSS) exists but isn't wired into the watchlist.
- Free Alpaca **IEX** data is thinner than SIP, especially premarket — some
  sub-$20 names show few/no bars until volume builds.
- The screener can briefly rate-limit; discovery retries and falls back to a
  liquid sub-$20 list, so the watchlist stays tradeable.
