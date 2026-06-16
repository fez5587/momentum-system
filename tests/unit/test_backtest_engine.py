"""Backtest engine regression tests.

The engine must evaluate setups against each bar's own timestamp, not the
wall clock. If it used datetime.now(), every setup in a session replayed
outside ~09:30-11:30 ET would be marked "late" past the entry cutoff and the
backtest would report zero trades — making the whole tool silently useless on
evenings and weekends.
"""

from datetime import datetime, timedelta

import pandas as pd

from strategy.backtest.engine import BacktestEngine


def _bars(specs, symbol="TEST"):
    # 09:30 ET expressed UTC-naive (matches how bars are stored), Friday session
    start = datetime(2026, 6, 12, 13, 30)
    rows = []
    cum_pv = cum_v = 0.0
    for i, (o, h, l, c, v) in enumerate(specs):
        ts = start + timedelta(minutes=i)
        typ = (h + l + c) / 3.0
        cum_pv += typ * v
        cum_v += v
        rows.append(
            dict(symbol=symbol, timestamp=ts, session_date=start.date(),
                 is_premarket=False, is_regular_hours=True,
                 open=o, high=h, low=l, close=c, volume=v,
                 vwap=cum_pv / cum_v if cum_v else c, quality_score=1.0)
        )
    return pd.DataFrame(rows)


def _momentum_day():
    """Gap-up base (warmup) -> impulse -> pullback -> breakout after bar 15."""
    specs = [
        (12.50, 12.62, 12.45, 12.55, 60_000), (12.55, 12.66, 12.50, 12.60, 52_000),
        (12.60, 12.70, 12.54, 12.64, 48_000), (12.64, 12.72, 12.58, 12.66, 46_000),
        (12.66, 12.74, 12.60, 12.68, 44_000), (12.68, 12.76, 12.62, 12.70, 43_000),
        (12.70, 12.78, 12.64, 12.72, 42_000), (12.72, 12.80, 12.66, 12.74, 41_000),
        (12.74, 12.82, 12.68, 12.76, 40_000), (12.76, 12.84, 12.70, 12.78, 40_000),
        (12.78, 12.86, 12.72, 12.80, 39_000), (12.80, 12.88, 12.74, 12.82, 38_000),
        (12.82, 12.90, 12.76, 12.84, 38_000), (12.84, 12.92, 12.78, 12.86, 37_000),
        (12.86, 12.94, 12.80, 12.88, 37_000), (12.88, 12.96, 12.82, 12.90, 36_000),
        (12.90, 13.20, 12.88, 13.16, 130_000), (13.16, 13.50, 13.14, 13.46, 165_000),
        (13.46, 13.80, 13.44, 13.76, 195_000), (13.76, 14.05, 13.74, 14.00, 210_000),
        (14.00, 14.04, 13.82, 13.86, 72_000), (13.86, 13.90, 13.74, 13.80, 58_000),
        (13.80, 13.86, 13.72, 13.78, 48_000), (13.78, 14.15, 13.76, 14.12, 235_000),
    ]
    price = specs[-1][3]
    for _ in range(16):
        o = price
        c = round(price + 0.10, 2)
        specs.append((o, round(max(o, c) + 0.04, 2), round(min(o, c) - 0.04, 2), c, 95_000))
        price = c
    return _bars(specs, "GAPR")


def test_backtest_signals_fire_regardless_of_wall_clock():
    # Runs at whatever real time the suite executes (often evenings/weekends).
    # If the engine used now(), this would be 0 signals / 0 trades.
    engine = BacktestEngine(equity=100_000.0)
    result = engine.run(
        _momentum_day(), "GAPR", previous_close=10.0, avg_daily_volume=500_000
    )
    assert result.signals >= 1, "engine found no setups — wall-clock cutoff bug?"
    assert len(result.trades) >= 1
    # each trade is a complete round trip with an exit reason
    for trade in result.trades:
        assert trade.exit_reason in {"target", "stop_loss", "session_end"}
        assert trade.entry_price > 0


def test_backtest_fader_produces_no_signal():
    """A steady downtrend below VWAP should never trigger a long."""
    specs = []
    price = 9.0
    for _ in range(34):
        o = price
        c = round(price - 0.07, 2)
        specs.append((o, round(o + 0.02, 2), round(c - 0.03, 2), c, 40_000))
        price = c
    engine = BacktestEngine(equity=100_000.0)
    result = engine.run(_bars(specs, "FADE"), "FADE",
                        previous_close=9.2, avg_daily_volume=400_000)
    assert result.signals == 0
    assert len(result.trades) == 0
