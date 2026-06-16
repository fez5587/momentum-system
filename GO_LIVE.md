# Go Live — market-open runbook

The system is verified end-to-end on real data (Postgres backend, sub-$20
discovery, live signals in the morning window). This is how to run and monitor
it at the open.

## 0. Preconditions (already done)
- Postgres reachable at `127.0.0.1:5432` (your LAN instance via the port proxy).
- `.env` has `DATABASE_URL=postgresql://admin:password@127.0.0.1:5432/momentum`
  and valid `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`.
- Linux venv at `/home/philip/.venvs/momentum`.

## 1. Start it (observe mode — safe, no orders)
```bash
cd /mnt/c/code/momentum
PYTHONPATH=. /home/philip/.venvs/momentum/bin/python run_live_paper.py --no-dashboard
```
What it does each loop: **discover** sub-$20 most-actives (every 5 min) →
**ingest** their 1-min bars (every 60s) → **watch**/evaluate (every 30s) → emit
events to Postgres. Stop with **Ctrl-C** (graceful).

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

## 4. Enable paper trading (only when you're ready)
Observe mode places **no orders**. To let it trade your Alpaca **paper** account,
set in `.env` and restart:
```
TRADING_EXECUTION_ENABLED=1
ALPACA_PAPER_SYNC_ENABLED=1
TRADING_AUTO_APPROVE=1      # 1 = auto-arm bracket orders; 0 = approve manually
```
With `TRADING_AUTO_APPROVE=0` you approve each signal in the dashboard (drop
`--no-dashboard`; it serves at `http://127.0.0.1:8765`). Risk per trade,
concurrency, and the reward multiple are the `TRADING_*` vars in `.env`.

> ⚠️ Known gap: the **daily-loss circuit breaker is NOT implemented**
> (`TRADING_MAX_DAILY_LOSS_PCT` is parsed but unused). If you enable execution,
> manage daily loss manually. (Surfaced by `momentum_cli.py doctor`.)

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
- Discovery can include leveraged ETFs (SOXS/TZA/TSLL/NVD); no ETF filter yet.
- News-catalyst discovery (RSS) exists but isn't wired into the watchlist.
- Free Alpaca **IEX** data is thinner than SIP, especially premarket — some
  sub-$20 names will show few/no bars until volume picks up.
