"""Backtest package: deterministic engine and outcome labeling."""

from strategy.backtest.engine import BacktestEngine, BacktestResult, Trade
from strategy.backtest.outcomes import label_outcome, OutcomeLabel

__all__ = ["BacktestEngine", "BacktestResult", "Trade", "label_outcome", "OutcomeLabel"]
