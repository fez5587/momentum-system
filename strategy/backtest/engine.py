"""Deterministic intraday backtest engine over minute bars.

Walks bars chronologically, evaluates setups, fills at next bar open with
configurable slippage/commission, and exits on stop, target, or session end.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pandas as pd

from config import BacktestConfig, Config
from strategy.evaluation.setup_evaluator import evaluate_setup
from strategy.exits import ExitConfig, simulate_exit
from strategy.risk.position_sizing import PositionSizingConfig, calculate_position_size


@dataclass
class Trade:
    trade_id: str
    symbol: str
    entry_time: str
    entry_price: float
    stop_price: float
    target_price: float
    quantity: int
    exit_time: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    realized_pnl: float | None = None
    r_multiple: float | None = None
    setup_type: str | None = None
    entry_index: int | None = None   # bar position of the entry-fill bar


@dataclass
class BacktestResult:
    symbol: str
    trades: list[Trade] = field(default_factory=list)
    total_pnl: float = 0.0
    win_rate: float = 0.0
    evaluations: int = 0
    signals: int = 0

    def summary(self) -> dict:
        wins = [t for t in self.trades if (t.realized_pnl or 0) > 0]
        return {
            "symbol": self.symbol,
            "trades": len(self.trades),
            "wins": len(wins),
            "win_rate": round(self.win_rate, 3),
            "total_pnl": round(self.total_pnl, 2),
            "evaluations": self.evaluations,
            "signals": self.signals,
        }


class BacktestEngine:
    """Single-symbol intraday backtest over a session of minute bars."""

    def __init__(
        self,
        config: Config | None = None,
        equity: float = 100_000.0,
        target_r: float = 2.0,
        warmup_bars: int = 15,
        eval_every: int = 5,
        ready_score_pct: float = 60.0,
        criteria=None,
        min_bars: int = 10,
        exit_config: ExitConfig | None = None,
    ):
        self.config = config or Config()
        self.bt: BacktestConfig = self.config.backtest
        self.equity = equity
        self.target_r = target_r
        self.warmup_bars = warmup_bars
        self.eval_every = eval_every
        self.ready_score_pct = ready_score_pct
        self.criteria = criteria
        self.min_bars = min_bars
        # default reproduces the original static bracket (stop / +target_r / EOD)
        self.exit_config = exit_config or ExitConfig(target_r=target_r)

    def _entry_costs(self, price: float, quantity: int) -> tuple[float, float]:
        slip = price * self.bt.slippage.base_spread_pct
        commission = max(
            self.bt.commission.minimum, quantity * self.bt.commission.per_share
        )
        return slip, commission

    def run(
        self,
        bars: pd.DataFrame,
        symbol: str,
        previous_close: float | None = None,
        avg_daily_volume: float | None = None,
    ) -> BacktestResult:
        result = BacktestResult(symbol=symbol)
        bars = bars.reset_index(drop=True)
        cfg = self.exit_config
        n = len(bars)

        i = self.warmup_bars
        while i < n - 1:
            if i % self.eval_every != 0:
                i += 1
                continue

            window = bars.iloc[: i + 1]
            # Evaluate as of the current bar's timestamp, not wall-clock now() —
            # otherwise every setup is judged against the real clock and marked
            # "late" past the entry cutoff (so a Sunday backtest would show zero
            # trades). Bars are stored UTC-naive; the cutoff reasons in
            # US/Eastern, so convert.
            eval_time = None
            try:
                from datetime import timezone as _tz
                from zoneinfo import ZoneInfo as _ZI

                last_ts = pd.Timestamp(window["timestamp"].iloc[-1]).to_pydatetime()
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=_tz.utc)
                eval_time = last_ts.astimezone(_ZI("America/New_York")).replace(
                    tzinfo=None
                )
            except Exception:  # noqa: BLE001 - fall back to now()
                eval_time = None

            evaluation = evaluate_setup(
                window,
                previous_close=previous_close,
                avg_daily_volume=avg_daily_volume,
                evaluation_time=eval_time,
                ready_score_pct=self.ready_score_pct,
                criteria=self.criteria,
                min_bars=self.min_bars,
            )
            result.evaluations += 1
            if not (evaluation.status == "ready" and evaluation.setups):
                i += 1
                continue

            result.signals += 1
            setup = evaluation.setups[0]
            entry_idx = i + 1
            next_bar = bars.iloc[entry_idx]
            raw_entry = (
                float(next_bar["open"])
                if self.bt.entry_mode == "next_bar"
                else float(bars.iloc[i]["close"])
            )
            stop = float(setup["stop_loss_price"])
            sizing = calculate_position_size(
                raw_entry, stop, equity=self.equity, config=PositionSizingConfig(),
            )
            if sizing.position_size <= 0:
                i += 1
                continue
            slip, entry_commission = self._entry_costs(raw_entry, sizing.position_size)
            entry = raw_entry + slip
            risk = entry - stop
            if risk <= 0:
                i += 1
                continue

            # Simulate the FULL managed exit forward with the shared rules (the
            # same logic the live exit manager applies), then resume scanning
            # after the exit bar so trades never overlap.
            bars_after = bars.iloc[entry_idx + 1:]
            res = simulate_exit(entry, stop, bars_after, cfg)
            qty = sizing.position_size
            gross = res.r_multiple * risk * qty
            total_commission = entry_commission
            exit_slip = 0.0
            for fll in res.fills:
                fqty = max(1, int(round(fll.frac * qty)))
                total_commission += max(
                    self.bt.commission.minimum, fqty * self.bt.commission.per_share
                )
                exit_slip += fll.price * self.bt.slippage.base_spread_pct * fqty
            realized = gross - total_commission - exit_slip

            exit_pos = min(entry_idx + 1 + res.exit_index, n - 1) if len(bars_after) else entry_idx
            trade = Trade(
                trade_id=str(uuid.uuid4()),
                symbol=symbol,
                entry_time=str(next_bar.get("timestamp", entry_idx)),
                entry_price=round(entry, 4),
                stop_price=round(stop, 4),
                target_price=round(entry + cfg.target_r * risk, 4) if cfg.target_r else round(entry, 4),
                quantity=qty,
                exit_time=str(bars.iloc[exit_pos].get("timestamp", exit_pos)),
                exit_price=round(res.fills[-1].price, 4) if res.fills else None,
                exit_reason=res.reason,
                realized_pnl=round(realized, 2),
                r_multiple=round(res.r_multiple, 3),
                setup_type=setup.get("setup_type"),
                entry_index=entry_idx,
            )
            result.trades.append(trade)
            result.total_pnl += trade.realized_pnl
            i = exit_pos + 1  # resume after the exit

        if result.trades:
            wins = sum(1 for t in result.trades if (t.realized_pnl or 0) > 0)
            result.win_rate = wins / len(result.trades)
        return result
