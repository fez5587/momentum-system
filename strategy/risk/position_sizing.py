"""Position sizing logic."""

from pydantic import BaseModel, Field


class PositionSizingConfig(BaseModel):
    """Configuration for position sizing."""

    risk_per_trade_pct: float = Field(
        default=0.01, description="Risk per trade as percent of equity"
    )
    default_equity: float = Field(
        default=100000.0, description="Default equity for sizing"
    )


class PositionSizeResult(BaseModel):
    """Result of position sizing calculation."""

    position_size: int = Field(description="Position size in shares")
    dollar_amount: float = Field(description="Dollar amount of position")
    risk_amount: float = Field(description="Dollar risk amount")


def calculate_position_size(
    entry_price: float,
    stop_loss_price: float,
    equity: float | None = None,
    config: PositionSizingConfig = PositionSizingConfig(),
    max_position_value: float | None = None,
    max_shares: int | None = None,
) -> PositionSizeResult:
    """Shares to risk ``risk_per_trade_pct`` of equity, capped by buying power
    and liquidity.

    - risk-based: shares = (equity * risk%) / (entry - stop)
    - ``max_position_value``: cap the position's DOLLAR value (buying-power aware
      — essential for a small account so one trade can't exceed available cash)
    - ``max_shares``: cap shares for LIQUIDITY (e.g. a % of the symbol's recent
      volume) so at size you don't move the market against yourself
    """
    account_equity = equity or config.default_equity
    risk_amount = account_equity * config.risk_per_trade_pct
    risk_per_share = abs(entry_price - stop_loss_price)

    if risk_per_share <= 0 or entry_price <= 0:
        return PositionSizeResult(position_size=0, dollar_amount=0.0, risk_amount=risk_amount)

    position_size = int(risk_amount / risk_per_share)
    if max_position_value is not None and max_position_value > 0:
        position_size = min(position_size, int(max_position_value / entry_price))
    if max_shares is not None and max_shares >= 0:
        position_size = min(position_size, int(max_shares))
    position_size = max(0, position_size)

    return PositionSizeResult(
        position_size=position_size,
        dollar_amount=position_size * entry_price,
        risk_amount=risk_amount,
    )
