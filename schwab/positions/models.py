"""Schwab account/position models."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AccountSummary:
    account_id: str
    account_desc: str = "Schwab"
    total_equity: float = 0.0
    cash_balance: float = 0.0
    buying_power: float = 0.0
    net_liquidating_value: float = 0.0
    is_fallback: bool = False


@dataclass
class Position:
    symbol: str
    quantity: float = 0.0
    avg_entry_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    asset_type: str = "EQUITY"


@dataclass
class AccountPositions:
    account_id: str
    positions: list[Position] = field(default_factory=list)
    is_fallback: bool = False
