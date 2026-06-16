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
) -> PositionSizeResult:
    """Calculate position size based on risk per trade."""
    account_equity = equity or config.default_equity
    risk_amount = account_equity * config.risk_per_trade_pct

    risk_per_share = abs(entry_price - stop_loss_price)

    if risk_per_share <= 0:
        return PositionSizeResult(
            position_size=0,
            dollar_amount=0.0,
            risk_amount=risk_amount,
        )

    position_size = int(risk_amount / risk_per_share)
    dollar_amount = position_size * entry_price

    return PositionSizeResult(
        position_size=position_size,
        dollar_amount=dollar_amount,
        risk_amount=risk_amount,
    )
