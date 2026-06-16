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
    ):
        self.config = config or Config()
        self.bt: BacktestConfig = self.config.backtest
        self.equity = equity
        self.target_r = target_r
        self.warmup_bars = warmup_bars
        self.eval_every = eval_every

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
        open_trade: Trade | None = None

        i = self.warmup_bars
        while i < len(bars) - 1:
            bar = bars.iloc[i]

            if open_trade is not None:
                low, high = float(bar["low"]), float(bar["high"])
                exit_price = None
                reason = None
                if low <= open_trade.stop_price:
                    exit_price, reason = open_trade.stop_price, "stop_loss"
                elif high >= open_trade.target_price:
                    exit_price, reason = open_trade.target_price, "target"
                elif i == len(bars) - 2:
                    exit_price, reason = float(bar["close"]), "session_end"
                if exit_price is not None:
                    self._close(open_trade, bar, exit_price, reason, result)
                    open_trade = None
                i += 1
                continue

            if i % self.eval_every == 0:
                window = bars.iloc[: i + 1]
                # Evaluate as of the current bar's timestamp, not wall-clock
                # now() — otherwise every setup is judged against the real
                # clock and marked "late" past the entry cutoff (so a Sunday
                # backtest would show zero trades). Bars are stored UTC-naive;
                # the cutoff reasons in US/Eastern, so convert.
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
                )
                result.evaluations += 1
                if evaluation.status == "ready" and evaluation.setups:
                    result.signals += 1
                    setup = evaluation.setups[0]
                    next_bar = bars.iloc[i + 1]
                    raw_entry = (
                        float(next_bar["open"])
                        if self.bt.entry_mode == "next_bar"
                        else float(bar["close"])
                    )
                    stop = float(setup["stop_loss_price"])
                    sizing = calculate_position_size(
                        raw_entry, stop, equity=self.equity,
                        config=PositionSizingConfig(),
                    )
                    if sizing.position_size > 0:
                        slip, commission = self._entry_costs(
                            raw_entry, sizing.position_size
                        )
                        entry = raw_entry + slip
                        risk = entry - stop
                        open_trade = Trade(
                            trade_id=str(uuid.uuid4()),
                            symbol=symbol,
                            entry_time=str(next_bar.get("timestamp", i + 1)),
                            entry_price=round(entry, 4),
                            stop_price=round(stop, 4),
                            target_price=round(entry + self.target_r * risk, 4),
                            quantity=sizing.position_size,
                        )
                        open_trade.realized_pnl = -commission
                        i += 1  # consumed next bar for entry
            i += 1

        if open_trade is not None:
            last = bars.iloc[-1]
            self._close(open_trade, last, float(last["close"]), "session_end", result)

        if result.trades:
            wins = sum(1 for t in result.trades if (t.realized_pnl or 0) > 0)
            result.win_rate = wins / len(result.trades)
        return result

    def _close(
        self, trade: Trade, bar, exit_price: float, reason: str | None,
        result: BacktestResult,
    ) -> None:
        slip, commission = self._entry_costs(exit_price, trade.quantity)
        fill = exit_price - slip
        gross = (fill - trade.entry_price) * trade.quantity
        trade.exit_time = str(bar.get("timestamp", "end"))
        trade.exit_price = round(fill, 4)
        trade.exit_reason = reason
        trade.realized_pnl = round((trade.realized_pnl or 0.0) + gross - commission, 2)
        risk = trade.entry_price - trade.stop_price
        trade.r_multiple = round((fill - trade.entry_price) / risk, 3) if risk > 0 else 0.0
        result.trades.append(trade)
        result.total_pnl += trade.realized_pnl
