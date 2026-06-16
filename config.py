"""Configuration loading from YAML files."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict


class SlippageConfig(BaseModel):
    base_spread_pct: float = 0.0015
    volatility_adder_pct: float = 0.0005
    atr_volatility_threshold: float = 2.0


class CommissionConfig(BaseModel):
    per_share: float = 0.005
    minimum: float = 1.00


class BacktestConfig(BaseModel):
    slippage: SlippageConfig = SlippageConfig()
    commission: CommissionConfig = CommissionConfig()
    entry_mode: str = "next_bar"


class ScannerWeights(BaseModel):
    gap_strength: float = 0.30
    relative_volume: float = 0.25
    premarket_participation: float = 0.20
    range_expansion: float = 0.15
    catalyst_freshness: float = 0.10


class ScannerConfig(BaseModel):
    gap_pct_min: float = 0.05
    premarket_volume_min: int = 250_000
    relative_volume_min: float = 2.5
    spread_pct_max: float = 0.05
    quality_score_min: float = 0.7
    weights: ScannerWeights = ScannerWeights()


class SetupConfig(BaseModel):
    impulse_size_min: float = 0.05
    max_pullback_depth_pct: float = 0.40
    breakout_volume_ratio_min: float = 1.5
    pullback_volume_ratio_max: float = 0.6
    timeframes: list[int] = [1, 5]


class RiskConfig(BaseModel):
    risk_per_trade_pct: float = 0.01
    max_daily_loss_pct: float = 0.03
    max_concurrent_positions: int = 3
    no_new_entries_after: str = "10:30"
    no_averaging_down: bool = True


class UniverseConfig(BaseModel):
    price_min: float = 1.0
    price_max: float = 20.0
    min_avg_volume_20d: int = 500_000
    exclude_etf: bool = True
    exclude_otc: bool = True
    common_shares_only: bool = True


class QualityConfig(BaseModel):
    min_regular_hours_coverage: float = 0.95
    min_premarket_coverage: float = 0.80
    max_price_gap_pct: float = 0.20
    min_score: float = 0.7


class DataConfig(BaseModel):
    dir: str = "./data"
    db_path: str = "./data/momentum.duckdb"


class TelemetryConfig(BaseModel):
    heartbeat_interval_seconds: int = 60
    log_dir: str = "./data/logs"


class ProviderQuotaConfig(BaseModel):
    """Per-provider API quota and rate limit settings."""

    model_config = ConfigDict(extra="ignore")

    fmp_daily_limit: int = 250
    fmp_intraday_limit: int = 100
    twelve_data_daily_limit: int = 800
    twelve_data_intraday_limit: int = 120
    alpha_vantage_daily_limit: int = 5
    newsapi_daily_limit: int = 100
    newsdata_daily_limit: int = 200
    marketaux_daily_limit: int = 100
    mediastack_daily_limit: int = 100


class RSSFeedConfig(BaseModel):
    """RSS feed source definitions."""

    model_config = ConfigDict(extra="ignore")

    feeds: list[str] = [
        "https://feeds.bloomberg.com/markets/news.rss",
        "https://feeds.marketwatch.com/marketwatch/topstories/",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://finance.yahoo.com/news/rssindex",
        "https://seekingalpha.com/feed.xml",
    ]


class NewsSourcesConfig(BaseModel):
    """News source configuration and cadence."""

    model_config = ConfigDict(extra="ignore")

    finnhub_enabled: bool = True
    finnhub_refresh_interval_seconds: int = 300
    rss_enabled: bool = True
    rss_refresh_interval_seconds: int = 600
    sec_enabled: bool = True
    sec_refresh_interval_seconds: int = 3600
    fed_enabled: bool = True
    fed_refresh_interval_seconds: int = 3600
    newsapi_enabled: bool = False
    newsapi_refresh_interval_seconds: int = 600
    newsdata_enabled: bool = False
    newsdata_refresh_interval_seconds: int = 600
    marketaux_enabled: bool = False
    marketaux_refresh_interval_seconds: int = 600
    mediastack_enabled: bool = False
    mediastack_refresh_interval_seconds: int = 600
    source_cooldown_default_seconds: int = 180
    provider_source_cooldown_seconds: int = 180
    rss_source_cooldown_seconds: int = 180
    regulatory_source_cooldown_seconds: int = 180
    rss_feeds: RSSFeedConfig = RSSFeedConfig()


class OllamaConfig(BaseModel):
    """Ollama local LLM settings for enrichment."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    host: str = "http://localhost:11434"
    model: str = "mistral"
    timeout_seconds: int = 30
    max_tokens: int = 256
    temperature: float = 0.3


class CapabilityRegistryConfig(BaseModel):
    """Capability registry configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True


class Config(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data: DataConfig = DataConfig()
    universe: UniverseConfig = UniverseConfig()
    scanner: ScannerConfig = ScannerConfig()
    setup: SetupConfig = SetupConfig()
    risk: RiskConfig = RiskConfig()
    backtest: BacktestConfig = BacktestConfig()
    quality: QualityConfig = QualityConfig()
    telemetry: TelemetryConfig = TelemetryConfig()
    providers: ProviderQuotaConfig = ProviderQuotaConfig()
    news_sources: NewsSourcesConfig = NewsSourcesConfig()
    ollama: OllamaConfig = OllamaConfig()
    capability_registry: CapabilityRegistryConfig = CapabilityRegistryConfig()


def load_config(path: str | Path | None = None) -> Config:
    """Load config from YAML file, falling back to defaults."""
    if path is None:
        path = Path("configs/base.yaml")
    path = Path(path)
    if path.exists():
        with open(path) as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}
        return Config(**raw)
    return Config()
