# Comprehensive Backtest Guide

This guide will help you run a multi-week backtest to assess the profitability and risk profile of the momentum strategy before trading live.

## Quick Start (Windows Terminal)

Run a 1-week backtest on small-cap momentum names (the strategy's sweet spot):

```powershell
uv run comprehensive_backtest.py --start 2026-06-05 --end 2026-06-12
```

This analyzes every trading day in that range, pulling minute bars for the specified symbols and reporting:
- **Daily P&L** — win/loss pattern by day
- **Symbol breakdown** — which tickers traded well/poorly
- **Trade-by-trade details** — entry/exit, profit/loss, reason for exit
- **Statistical summary** — win rate, average R-multiple, profit factor, risk metrics
- **Loss analysis** — patterns in losses (gap-throughs, wide stops, etc.)

## What the Output Tells You

### Summary Metrics

- **Win Rate**: % of trades that were profitable. 40%+ is acceptable; 50%+ is strong.
- **Profit Factor**: Total wins ÷ absolute value of total losses. 
  - < 1.3: marginal edge or slippage issues (investigate before live)
  - 1.3–2.0: decent edge
  - > 2.0: strong edge
- **Avg R-Multiple**: Average return per unit of risk. 1.5+ suggests the strategy rewards you for the risk taken.
- **Max Loss**: Largest single loss. If it's 2–3× worse than your average loss, stops may be too wide or slippage is severe.

### Red Flags to Watch For

1. **Profit factor < 1.3** — your losses are eating most of your wins
2. **Win rate < 40%** — even with 2:1 R ratio, this struggles to be profitable
3. **Max loss >> average loss** — suggests gap-throughs or wide stops on losers
4. **Inconsistent daily results** — if one bad day wipes out a week's gains, volatility is too high

## Advanced Options

```powershell
# Test a different date range (e.g., last 2 weeks)
uv run comprehensive_backtest.py --start 2026-05-27 --end 2026-06-12

# Test on your watched symbols instead of the defaults
uv run comprehensive_backtest.py --start 2026-06-05 --end 2026-06-12 --symbols SNDL,MARA,RIOT

# Test a full month (if available)
uv run comprehensive_backtest.py --start 2026-05-13 --end 2026-06-12
```

## Important Caveats

1. **This is NOT the live execution path** — the backtest engine shows signal→entry→exit on historical bars, but it does NOT exercise:
   - The new auto-arm resting-limit logic
   - The tick-by-tick entry invalidation (live prices)
   - The approval queue or manual trading decisions
   
   All of those features only run in `uv run run_live_paper.py --auto-approve`. This backtest proves the signal-generation and position-management logic works; it's not a full replay of the live system.

2. **IEX feed gaps** — free tier only sees IEX trades (a subset of consolidated). On illiquid small-caps there may be minutes with no prints, causing simulated fills to happen on stale data. The paid SIP feed (ALPACA_DATA_FEED=sip) would see more ticks.

3. **Minute-bar vs tick-by-tick** — the backtest assumes fills happen at the bar open of the next bar. Live, entries/exits are much more granular (actual filled prices as they happen). This is usually conservative (against you) but can help on some exits.

4. **Slippage assumptions** — the engine includes configurable slippage (base_spread_pct in config). The default assumes ~0.05% slippage; real fills vary by size, liquidity, and market conditions.

## Interpretation Guide

### Strong Strategy (Should Trade)
- Win rate ≥ 45%
- Profit factor ≥ 1.8
- Avg R ≥ 1.5R
- Max loss ≤ 2× average loss
- Consistent week-to-week results

### Marginal Strategy (Investigate Further)
- Win rate 40–45%
- Profit factor 1.3–1.8
- Avg R ≈ 1.0–1.5R
- Max loss 2–3× average loss
- High daily variance

### Weak Strategy (Do Not Trade)
- Win rate < 40%
- Profit factor < 1.3
- Avg R < 0.5R
- Max loss > 3× average loss
- Losing weeks common

## Next Steps

1. **Run the backtest** for the most recent 2–4 weeks using small-cap symbols (SNDL, MARA, RIOT, BBAI, SOUN, PLUG).
2. **Review the loss analysis** — click "Loss Analysis" section and understand why you're losing. Is it:
   - Entries are wrong (enter on fakeouts)? → Tighten entry criteria
   - Stops are too wide (gap-throughs)? → Use a tighter stop or accept wider slippage
   - Exits are leaving money? → Adjust target or exit rules
3. **Check the daily pattern** — if one symbol dominates losses, exclude it (maybe illiquid on IEX).
4. **If strong** → uv run run_live_paper.py --auto-approve on the next trading day.
5. **If marginal/weak** → dig into the top losers to understand the pattern before risking real capital.

## Debugging a Bad Backtest

If you see 0 trades despite seeing signals:

```powershell
# First, make sure minute bars were fetched
uv run fetch_minute_bars.py SNDL MARA RIOT --lookback 5000

# Then try a single-symbol, single-day backtest to see detailed output
uv run backtest_cli.py --symbol SNDL --date 2026-06-12
```

If you get "no symbols with minute bars," the date may not have tradeable data (check it's a weekday) or the lookback was too short.

## Questions?

- The backtest engine: `strategy/backtest/engine.py`
- Setup evaluation (what defines a "signal"): `strategy/evaluation/setup_evaluator.py`
- Risk/position sizing: `strategy/risk/position_sizing.py`
- Config defaults: `config.py` (BacktestConfig section)
