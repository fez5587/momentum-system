"""Tradeable universe filtering (price band, volume, share class)."""

from __future__ import annotations

from config import UniverseConfig


def symbol_in_universe(
    price: float,
    avg_volume_20d: float | None = None,
    is_etf: bool = False,
    is_otc: bool = False,
    config: UniverseConfig | None = None,
) -> tuple[bool, str | None]:
    """Check whether a symbol qualifies for the trading universe."""
    config = config or UniverseConfig()
    if price < config.price_min or price > config.price_max:
        return False, f"price {price:.2f} outside [{config.price_min}, {config.price_max}]"
    if (
        avg_volume_20d is not None
        and avg_volume_20d < config.min_avg_volume_20d
    ):
        return False, f"avg volume {avg_volume_20d:,.0f} < {config.min_avg_volume_20d:,}"
    if config.exclude_etf and is_etf:
        return False, "ETF excluded"
    if config.exclude_otc and is_otc:
        return False, "OTC excluded"
    return True, None
