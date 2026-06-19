-- PostgreSQL schema for the momentum system (single datastore).
--
-- Port of storage/schema.py (DuckDB) + the event store `events` table that
-- previously lived in storage/event_store.py. Dialect deltas applied:
--   DOUBLE            -> DOUBLE PRECISION
--   payload index     -> on event_type/correlation_id (a btree on unbounded
--                        TEXT payload is invalid in Postgres)
--   everything else   -> already Postgres-compatible (DuckDB dialect is
--                        Postgres-derived: IF NOT EXISTS, BOOLEAN, VARCHAR,
--                        composite PKs, current_timestamp defaults all work).
--
-- Idempotent: safe to run repeatedly (CREATE ... IF NOT EXISTS throughout).

-- =====================================================================
-- Operational event store (append-only source of truth)
-- =====================================================================
CREATE TABLE IF NOT EXISTS events (
    id VARCHAR PRIMARY KEY,
    timestamp TIMESTAMP,
    mode VARCHAR,
    event_type VARCHAR,
    correlation_id VARCHAR,
    message VARCHAR,
    payload_json TEXT,
    created_at TIMESTAMP DEFAULT current_timestamp
);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp);
CREATE INDEX IF NOT EXISTS idx_events_correlation ON events (correlation_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type);

-- =====================================================================
-- Universe metadata
-- =====================================================================
CREATE TABLE IF NOT EXISTS symbols (
    symbol VARCHAR PRIMARY KEY,
    security_name VARCHAR,
    exchange VARCHAR,
    asset_type VARCHAR DEFAULT 'common_stock',
    sector VARCHAR,
    industry VARCHAR,
    shares_outstanding BIGINT,
    float_shares BIGINT,
    market_cap DOUBLE PRECISION,
    is_etf BOOLEAN DEFAULT FALSE,
    is_otc BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS trading_days (
    trade_date DATE PRIMARY KEY,
    session_open TIMESTAMP,
    session_close TIMESTAMP,
    premarket_open TIMESTAMP,
    afterhours_close TIMESTAMP,
    is_half_day BOOLEAN DEFAULT FALSE,
    is_holiday_adjacent BOOLEAN DEFAULT FALSE,
    tape_regime VARCHAR,
    market_regime VARCHAR,
    volatility_regime VARCHAR,
    gap_activity_level INTEGER,
    momentum_density INTEGER
);

-- =====================================================================
-- Bars
-- =====================================================================
CREATE TABLE IF NOT EXISTS daily_bars (
    symbol VARCHAR,
    trade_date DATE,
    open DOUBLE PRECISION,
    high DOUBLE PRECISION,
    low DOUBLE PRECISION,
    close DOUBLE PRECISION,
    volume BIGINT,
    vwap DOUBLE PRECISION,
    previous_close DOUBLE PRECISION,
    true_range DOUBLE PRECISION,
    atr_14 DOUBLE PRECISION,
    rolling_avg_volume_20d DOUBLE PRECISION,
    rolling_avg_volume_50d DOUBLE PRECISION,
    prior_day_gain_pct DOUBLE PRECISION,
    multi_day_runner_flag BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS minute_bars (
    symbol VARCHAR,
    timestamp TIMESTAMP,
    session_date DATE,
    is_premarket BOOLEAN,
    is_regular_hours BOOLEAN,
    is_afterhours BOOLEAN,
    open DOUBLE PRECISION,
    high DOUBLE PRECISION,
    low DOUBLE PRECISION,
    close DOUBLE PRECISION,
    volume BIGINT,
    vwap DOUBLE PRECISION,
    spread_pct DOUBLE PRECISION,
    halt_status BOOLEAN DEFAULT FALSE,
    source_provider VARCHAR DEFAULT 'synthetic',
    quality_score DOUBLE PRECISION,
    PRIMARY KEY (symbol, timestamp)
);
-- Fast per-symbol/session bar lookups (the `inspect bars` path).
CREATE INDEX IF NOT EXISTS idx_minute_bars_symbol_session
    ON minute_bars (symbol, session_date);

-- =====================================================================
-- News / catalysts / halts / context
-- =====================================================================
CREATE TABLE IF NOT EXISTS news_events (
    id VARCHAR,
    symbol VARCHAR,
    headline VARCHAR,
    source VARCHAR,
    published_at TIMESTAMP,
    catalyst_freshness_minutes DOUBLE PRECISION,
    category VARCHAR,
    sentiment DOUBLE PRECISION,
    is_earnings BOOLEAN DEFAULT FALSE,
    is_offering BOOLEAN DEFAULT FALSE,
    is_halt_related BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS halt_events (
    id VARCHAR,
    symbol VARCHAR,
    halt_start TIMESTAMP,
    halt_end TIMESTAMP,
    halt_type VARCHAR,
    price_at_halt DOUBLE PRECISION,
    resumed_price DOUBLE PRECISION,
    gap_on_resume_pct DOUBLE PRECISION,
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS market_context (
    timestamp TIMESTAMP,
    trade_date DATE,
    spy_open DOUBLE PRECISION,
    spy_high DOUBLE PRECISION,
    spy_low DOUBLE PRECISION,
    spy_close DOUBLE PRECISION,
    spy_volume BIGINT,
    qqq_open DOUBLE PRECISION,
    qqq_close DOUBLE PRECISION,
    spy_intraday_return_pct DOUBLE PRECISION,
    vix_open DOUBLE PRECISION,
    vix_level DOUBLE PRECISION,
    advance_decline_ratio DOUBLE PRECISION,
    up_volume_down_volume_ratio DOUBLE PRECISION,
    tape_regime_flag VARCHAR,
    PRIMARY KEY (timestamp)
);

CREATE TABLE IF NOT EXISTS dilution_events (
    id VARCHAR PRIMARY KEY,
    symbol VARCHAR,
    event_type VARCHAR,
    announced_at TIMESTAMP,
    effective_date DATE,
    share_count BIGINT,
    price_per_share DOUBLE PRECISION,
    gross_proceeds DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS short_interest (
    symbol VARCHAR,
    report_date DATE,
    short_interest_shares BIGINT,
    short_interest_pct_float DOUBLE PRECISION,
    days_to_cover DOUBLE PRECISION,
    PRIMARY KEY (symbol, report_date)
);

-- =====================================================================
-- Features / setups / outcomes / backtests
-- =====================================================================
CREATE TABLE IF NOT EXISTS engineered_features (
    id VARCHAR PRIMARY KEY,
    timestamp TIMESTAMP,
    symbol VARCHAR,
    session_date DATE,
    feature_version VARCHAR,
    gap_pct DOUBLE PRECISION,
    premarket_gap_pct DOUBLE PRECISION,
    relative_volume DOUBLE PRECISION,
    time_of_day_adjusted_relative_volume DOUBLE PRECISION,
    float_rotation_pct DOUBLE PRECISION,
    vwap DOUBLE PRECISION,
    distance_from_vwap DOUBLE PRECISION,
    ema9 DOUBLE PRECISION,
    ema20 DOUBLE PRECISION,
    pullback_volume_ratio DOUBLE PRECISION,
    pullback_candle_type VARCHAR,
    catalyst_freshness_minutes DOUBLE PRECISION,
    catalyst_type VARCHAR,
    tape_regime VARCHAR,
    spy_intraday_return DOUBLE PRECISION,
    vix_level DOUBLE PRECISION,
    metadata_json VARCHAR
);

CREATE TABLE IF NOT EXISTS setup_events (
    setup_id VARCHAR PRIMARY KEY,
    symbol VARCHAR,
    setup_time TIMESTAMP,
    session_date DATE,
    setup_name VARCHAR DEFAULT 'first_pullback',
    setup_version VARCHAR DEFAULT 'v1',
    entry_reference_price DOUBLE PRECISION,
    invalidation_price DOUBLE PRECISION,
    target_r_multiple DOUBLE PRECISION DEFAULT 2.0,
    impulse_start_price DOUBLE PRECISION,
    impulse_end_price DOUBLE PRECISION,
    impulse_pct DOUBLE PRECISION,
    pullback_low DOUBLE PRECISION,
    pullback_depth_pct DOUBLE PRECISION,
    pullback_bars INTEGER,
    pullback_volume_ratio DOUBLE PRECISION,
    above_vwap_flag BOOLEAN,
    vwap_at_trigger DOUBLE PRECISION,
    gap_pct DOUBLE PRECISION,
    relative_volume DOUBLE PRECISION,
    float_rotation_at_trigger DOUBLE PRECISION,
    catalyst_freshness_at_trigger DOUBLE PRECISION,
    session_minute_number INTEGER,
    tape_regime_at_trigger VARCHAR,
    spy_intraday_return_pct_at_trigger DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS outcome_labels (
    setup_id VARCHAR PRIMARY KEY,
    label_version VARCHAR DEFAULT 'v1',
    max_upside_next_5m DOUBLE PRECISION,
    max_upside_next_15m DOUBLE PRECISION,
    max_upside_next_60m DOUBLE PRECISION,
    max_drawdown_next_5m DOUBLE PRECISION,
    max_drawdown_next_15m DOUBLE PRECISION,
    max_drawdown_next_60m DOUBLE PRECISION,
    reached_1r_before_minus_1r BOOLEAN,
    reached_2r_before_minus_1r BOOLEAN,
    held_vwap_5m BOOLEAN,
    held_vwap_15m BOOLEAN,
    trend_day_flag BOOLEAN,
    failed_breakout_flag BOOLEAN,
    time_to_max_upside_minutes DOUBLE PRECISION,
    time_to_max_drawdown_minutes DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id VARCHAR PRIMARY KEY,
    run_timestamp TIMESTAMP,
    strategy_name VARCHAR,
    strategy_version VARCHAR,
    feature_version VARCHAR,
    date_range_start DATE,
    date_range_end DATE,
    parameter_config_json VARCHAR,
    summary_metrics_json VARCHAR,
    total_trades INTEGER,
    win_rate DOUBLE PRECISION,
    expectancy DOUBLE PRECISION,
    max_drawdown DOUBLE PRECISION,
    profit_factor DOUBLE PRECISION
);

-- =====================================================================
-- Paper trading
-- =====================================================================
CREATE TABLE IF NOT EXISTS simulated_orders (
    order_id VARCHAR PRIMARY KEY,
    session_id VARCHAR,
    symbol VARCHAR,
    created_at TIMESTAMP,
    side VARCHAR,
    order_type VARCHAR,
    qty INTEGER,
    intended_entry DOUBLE PRECISION,
    stop_price DOUBLE PRECISION,
    target_price DOUBLE PRECISION,
    status VARCHAR,
    strategy_name VARCHAR,
    setup_id VARCHAR
);

CREATE TABLE IF NOT EXISTS simulated_fills (
    fill_id VARCHAR PRIMARY KEY,
    order_id VARCHAR,
    symbol VARCHAR,
    fill_time TIMESTAMP,
    fill_price DOUBLE PRECISION,
    qty INTEGER,
    slippage DOUBLE PRECISION,
    commission DOUBLE PRECISION,
    venue VARCHAR
);

CREATE TABLE IF NOT EXISTS strategy_runs (
    run_id VARCHAR PRIMARY KEY,
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    mode VARCHAR,
    strategy_name VARCHAR,
    strategy_version VARCHAR,
    config_json VARCHAR,
    summary_json VARCHAR
);

CREATE TABLE IF NOT EXISTS paper_sessions (
    session_id VARCHAR PRIMARY KEY,
    trade_date DATE,
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    starting_equity DOUBLE PRECISION,
    ending_equity DOUBLE PRECISION,
    realized_pnl DOUBLE PRECISION,
    unrealized_pnl DOUBLE PRECISION,
    total_orders INTEGER,
    total_fills INTEGER,
    total_setups INTEGER,
    notes VARCHAR
);

CREATE TABLE IF NOT EXISTS paper_missed_trades (
    audit_id VARCHAR PRIMARY KEY,
    session_id VARCHAR,
    symbol VARCHAR,
    setup_id VARCHAR,
    reason VARCHAR,
    setup_time TIMESTAMP,
    evaluated_at TIMESTAMP,
    attempted_entry_time TIMESTAMP,
    entry_cutoff VARCHAR,
    minutes_past_cutoff DOUBLE PRECISION,
    success_score_pct DOUBLE PRECISION,
    criteria_passed INTEGER,
    criteria_total INTEGER,
    provider_status VARCHAR,
    provider_degraded_count INTEGER,
    source_status VARCHAR,
    source_degraded_count INTEGER,
    source_backoff_count INTEGER,
    created_at TIMESTAMP,
    details_json VARCHAR
);

-- =====================================================================
-- Raw landing (append-only) + telemetry + scanner snapshots
-- =====================================================================
CREATE TABLE IF NOT EXISTS raw_fetch_attempts (
    id VARCHAR PRIMARY KEY,
    source VARCHAR NOT NULL,
    capability VARCHAR NOT NULL,
    request_window_start TIMESTAMP,
    request_window_end TIMESTAMP,
    fetched_at TIMESTAMP NOT NULL,
    ingest_run_id VARCHAR,
    http_status INTEGER,
    item_count INTEGER,
    error_msg VARCHAR,
    payload_hash VARCHAR,
    parser_version VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_raw_fetch_attempts_source_window
    ON raw_fetch_attempts (source, capability, request_window_start, request_window_end);
CREATE INDEX IF NOT EXISTS idx_raw_fetch_attempts_ingest_run
    ON raw_fetch_attempts (ingest_run_id);

CREATE TABLE IF NOT EXISTS raw_news_items (
    id VARCHAR PRIMARY KEY,
    fetch_attempt_id VARCHAR,
    source VARCHAR,
    raw_url VARCHAR,
    raw_title VARCHAR,
    raw_published_at VARCHAR,
    raw_body_snippet VARCHAR,
    raw_tickers VARCHAR,
    payload_hash VARCHAR,
    parser_version VARCHAR,
    fetched_at TIMESTAMP,
    ingest_run_id VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_raw_news_items_fetch_attempt
    ON raw_news_items (fetch_attempt_id);
CREATE INDEX IF NOT EXISTS idx_raw_news_items_source_published
    ON raw_news_items (source, raw_published_at);

-- Local-LLM (Ollama) catalyst enrichment cache. One row per (headline, ticker);
-- headline_hash = raw_news_items.payload_hash (sha256(source|url|title)) so we
-- dedupe and join for free. Read model for the dashboard advisory + Phase 2.
CREATE TABLE IF NOT EXISTS news_catalyst_cache (
    headline_hash   VARCHAR,
    symbol          VARCHAR,
    headline        VARCHAR,
    source          VARCHAR,
    catalyst_type   VARCHAR,
    sentiment       DOUBLE PRECISION,
    conviction      DOUBLE PRECISION,
    is_dilutive     BOOLEAN DEFAULT FALSE,
    rationale       VARCHAR,
    model           VARCHAR,
    enriched_at     TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (headline_hash, symbol)
);
CREATE INDEX IF NOT EXISTS idx_news_catalyst_cache_symbol
    ON news_catalyst_cache (symbol);
CREATE INDEX IF NOT EXISTS idx_news_catalyst_cache_enriched
    ON news_catalyst_cache (enriched_at);

CREATE TABLE IF NOT EXISTS telemetry_events (
    id VARCHAR PRIMARY KEY,
    event_type VARCHAR,
    session_id VARCHAR,
    symbol VARCHAR,
    source VARCHAR,
    from_state VARCHAR,
    to_state VARCHAR,
    reason VARCHAR,
    metadata_json VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS scanner_snapshots (
    id VARCHAR PRIMARY KEY,
    snapshot_time TIMESTAMP,
    symbol VARCHAR,
    rank INTEGER,
    price DOUBLE PRECISION,
    gap_pct DOUBLE PRECISION,
    premarket_gap_pct DOUBLE PRECISION,
    cumulative_volume BIGINT,
    minute_volume BIGINT,
    premarket_volume BIGINT,
    relative_volume DOUBLE PRECISION,
    float_rotation_pct DOUBLE PRECISION,
    vwap DOUBLE PRECISION,
    distance_from_vwap DOUBLE PRECISION,
    ema9 DOUBLE PRECISION,
    ema20 DOUBLE PRECISION,
    intraday_range DOUBLE PRECISION,
    intraday_range_vs_atr DOUBLE PRECISION,
    distance_to_premarket_high DOUBLE PRECISION,
    distance_to_premarket_low DOUBLE PRECISION,
    distance_to_day_high DOUBLE PRECISION,
    distance_to_day_low DOUBLE PRECISION,
    news_flag BOOLEAN,
    catalyst_freshness_minutes DOUBLE PRECISION,
    catalyst_type VARCHAR,
    float_bucket VARCHAR,
    market_cap_bucket VARCHAR,
    spread_pct DOUBLE PRECISION,
    halt_flag BOOLEAN,
    dilution_risk_flag BOOLEAN,
    sympathy_move_candidate_flag BOOLEAN,
    lead_runner_symbol VARCHAR,
    sympathy_cluster_id VARCHAR,
    early_leader_flag BOOLEAN,
    rank_stability DOUBLE PRECISION,
    momentum_score DOUBLE PRECISION,
    scanner_version VARCHAR
);

-- =====================================================================
-- Late-added columns (parity with storage/schema.py:create_schema)
-- Postgres supports ADD COLUMN IF NOT EXISTS (9.6+).
-- =====================================================================
ALTER TABLE setup_events ADD COLUMN IF NOT EXISTS tape_regime_at_trigger VARCHAR;
ALTER TABLE setup_events ADD COLUMN IF NOT EXISTS spy_intraday_return_pct_at_trigger DOUBLE PRECISION;
