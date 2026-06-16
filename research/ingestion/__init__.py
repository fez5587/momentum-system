"""Research ingestion: bars, news, signal scans, and watcher providers."""

from research.ingestion.market_data import (  # noqa: F401
    IngestionResult,
    classify_session,
    discover_active_symbols,
    ingest_daily_history,
    ingest_live_minute_bars,
)
from research.ingestion.scheduler import Scheduler, ScheduledTask  # noqa: F401
from research.ingestion.signals import scan_gappers, store_scanner_snapshot  # noqa: F401
from research.ingestion.watcher_task import (  # noqa: F401
    LiveWatchlistProvider,
    ResearchWatchlistProvider,
    run_watcher_tick,
)
