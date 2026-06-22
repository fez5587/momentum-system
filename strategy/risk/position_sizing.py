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
    max_risk_dollars: float | None = None,
) -> PositionSizeResult:
    """Shares to risk ``risk_per_trade_pct`` of equity, capped by buying power
    and liquidity.

    - risk-based: shares = (equity * risk%) / (entry - stop)
    - ``max_risk_dollars``: HARD cap on the trade's dollar risk regardless of
      equity/stop width — fixed-fractional with a ceiling. Without it the
      percentage budget alone let wide-stop names risk ~3x the median (the
      BTBT/WKSP/LNKS ~-$1k losers); this makes every trade risk
      min(equity*risk%, max_risk_dollars).
    - ``max_position_value``: cap the position's DOLLAR value (buying-power aware
      — essential for a small account so one trade can't exceed available cash)
    - ``max_shares``: cap shares for LIQUIDITY (e.g. a % of the symbol's recent
      volume) so at size you don't move the market against yourself
    """
    account_equity = equity or config.default_equity
    risk_amount = account_equity * config.risk_per_trade_pct
    # fixed-fractional with a hard ceiling: the dollar risk used to SIZE is the
    # smaller of the % budget and the absolute cap.
    if max_risk_dollars is not None and max_risk_dollars > 0:
        risk_amount = min(risk_amount, max_risk_dollars)
    risk_per_share = abs(entry_price - stop_loss_price)

    if risk_per_share <= 0 or entry_price <= 0:
        return PositionSizeResult(position_size=0, dollar_amount=0.0, risk_amount=0.0)

    position_size = int(risk_amount / risk_per_share)
    if max_position_value is not None and max_position_value > 0:
        position_size = min(position_size, int(max_position_value / entry_price))
    if max_shares is not None and max_shares >= 0:
        position_size = min(position_size, int(max_shares))
    position_size = max(0, position_size)

    return PositionSizeResult(
        position_size=position_size,
        dollar_amount=position_size * entry_price,
        # the ACTUAL dollar risk of the sized position (after every cap), so the
        # caller/audit sees what's truly at stake, not just the budget.
        risk_amount=round(position_size * risk_per_share, 2),
    )
