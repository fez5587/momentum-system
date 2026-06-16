"""Provider registry for market-data sources.

(Repaired: ProviderConfig is a proper top-level dataclass and the registry
initializes its mapping correctly — the original nested the dataclass inside
the enum and used an invalid `self.providers: {}` annotation-as-assignment.)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum


class ProviderKind(str, Enum):
    """Supported market-data providers."""

    ALPACA = "alpaca"
    SCHWAB = "schwab"
    POLYGON = "polygon"
    FINNHUB = "finnhub"
    SYNTHETIC = "synthetic"


@dataclass
class ProviderConfig:
    """Configuration for one provider."""

    kind: ProviderKind
    enabled: bool = True
    priority: int = 100  # lower = preferred
    rate_limit_per_minute: int = 200
    supports_minute_bars: bool = True
    supports_quotes: bool = True
    env_keys: tuple[str, ...] = field(default_factory=tuple)

    def is_configured(self, env: dict[str, str] | None = None) -> bool:
        """A provider is configured when all of its env keys are present."""
        values = dict(os.environ)
        if env is not None:
            values.update(env)
        return all(values.get(key) for key in self.env_keys)


class ProviderRegistry:
    """Registry of provider configs with priority-based selection."""

    def __init__(self) -> None:
        self.providers: dict[ProviderKind, ProviderConfig] = {}

    def register(self, config: ProviderConfig) -> None:
        self.providers[config.kind] = config

    def get(self, kind: ProviderKind | str) -> ProviderConfig | None:
        if isinstance(kind, str):
            try:
                kind = ProviderKind(kind)
            except ValueError:
                return None
        return self.providers.get(kind)

    def enabled_providers(self) -> list[ProviderConfig]:
        return sorted(
            (p for p in self.providers.values() if p.enabled),
            key=lambda p: p.priority,
        )

    def best_configured(
        self, env: dict[str, str] | None = None
    ) -> ProviderConfig | None:
        for provider in self.enabled_providers():
            if provider.is_configured(env):
                return provider
        return None


def default_registry() -> ProviderRegistry:
    """Registry with the standard provider stack."""
    registry = ProviderRegistry()
    registry.register(
        ProviderConfig(
            kind=ProviderKind.ALPACA,
            priority=10,
            env_keys=("ALPACA_API_KEY", "ALPACA_SECRET_KEY"),
        )
    )
    registry.register(
        ProviderConfig(
            kind=ProviderKind.SCHWAB,
            priority=20,
            env_keys=("SCHWAB_MARKET_DATA_APP_KEY", "SCHWAB_MARKET_DATA_APP_SECRET"),
        )
    )
    registry.register(
        ProviderConfig(
            kind=ProviderKind.POLYGON, priority=30, env_keys=("POLYGON_API_KEY",)
        )
    )
    registry.register(
        ProviderConfig(
            kind=ProviderKind.FINNHUB, priority=40, env_keys=("FINNHUB_API_KEY",)
        )
    )
    registry.register(
        ProviderConfig(
            kind=ProviderKind.SYNTHETIC,
            priority=1000,
            env_keys=(),
            supports_quotes=False,
        )
    )
    return registry
