"""Configuration loading from YAML files."""

import os
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
    # Datastore is Postgres (DATABASE_URL); this default is a legacy fallback
    # only — the connection layer ignores the path. See storage.db_pg.
    db_path: str = "momentum"


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
    """Ollama local LLM settings for news/catalyst enrichment."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    host: str = "http://localhost:11434"
    # qwen2.5:7b-instruct (Q4, ~4.7GB) fits a 12GB GPU with headroom and has the
    # best small-model strict-JSON adherence. Alts: llama3.1:8b-instruct-q4_K_M,
    # mistral:7b-instruct, qwen2.5:3b-instruct (throughput).
    model: str = "qwen2.5:7b-instruct"
    timeout_seconds: int = 30
    max_tokens: int = 256
    temperature: float = 0.3
    # News/catalyst enrichment (advisory layer).
    enrichment_enabled: bool = False
    enrichment_interval_seconds: int = 120
    enrichment_lookback_hours: int = 12
    enrichment_batch_limit: int = 50  # max headlines per pass (GPU budget)
    # Phase 2 dilution veto (ships OFF).
    dilution_veto_enabled: bool = False
    dilution_veto_min_conviction: float = 0.6
    # Phase 2 score blend (ships OFF; separate from enrichment so turning on the
    # advisory/dashboard does NOT silently change trade scoring).
    catalyst_score_enabled: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "OllamaConfig":
        """Build from OLLAMA_* / NEWS_* env vars (run_live_paper reads env, not YAML)."""
        v = dict(os.environ)
        if env:
            v.update(env)

        def b(key: str, default: bool) -> bool:
            raw = v.get(key)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        def i(key: str, default: int) -> int:
            try:
                return int(v.get(key, default))
            except (TypeError, ValueError):
                return default

        def f(key: str, default: float) -> float:
            try:
                return float(v.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=b("OLLAMA_ENABLED", False),
            host=v.get("OLLAMA_HOST", cls.model_fields["host"].default),
            model=v.get("OLLAMA_MODEL", cls.model_fields["model"].default),
            timeout_seconds=i("OLLAMA_TIMEOUT_SECONDS", 30),
            max_tokens=i("OLLAMA_MAX_TOKENS", 256),
            temperature=f("OLLAMA_TEMPERATURE", 0.3),
            enrichment_enabled=b("NEWS_ENRICH_ENABLED", False),
            enrichment_interval_seconds=i("NEWS_ENRICH_INTERVAL_SECONDS", 120),
            enrichment_lookback_hours=i("NEWS_ENRICH_LOOKBACK_HOURS", 12),
            enrichment_batch_limit=i("NEWS_ENRICH_BATCH_LIMIT", 50),
            dilution_veto_enabled=b("NEWS_DILUTION_VETO_ENABLED", False),
            dilution_veto_min_conviction=f("NEWS_DILUTION_VETO_CONVICTION", 0.6),
            catalyst_score_enabled=b("NEWS_CATALYST_SCORE_ENABLED", False),
        )


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
