"""DuckDB schema definitions for all tables.

Milestone 1, Task 1.1
"""

SCHEMA_SQL = """
-- Symbols: universe metadata
CREATE TABLE IF NOT EXISTS symbols (
    symbol VARCHAR PRIMARY KEY,
    security_name VARCHAR,
    exchange VARCHAR,
    asset_type VARCHAR DEFAULT 'common_stock',
    sector VARCHAR,
    industry VARCHAR,
    shares_outstanding BIGINT,
    float_shares BIGINT,
    market_cap DOUBLE,
    is_etf BOOLEAN DEFAULT FALSE,
    is_otc BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP DEFAULT current_timestamp
);

-- Trading days: market calendar
CREATE TABLE IF NOT EXISTS trading_days (
    trade_date DATE PRIMARY KEY,
    session_open TIMESTAMP,
    session_close TIMESTAMP,
    premarket_open TIMESTAMP,
    afterhours_close TIMESTAMP,
    is_half_day BOOLEAN DEFAULT FALSE,
    is_holiday_adjacent BOOLEAN DEFAULT FALSE,
    tape_regime VARCHAR,        -- strong / neutral / weak
    market_regime VARCHAR,      -- bull / neutral / bear
    volatility_regime VARCHAR,  -- low / medium / high
    gap_activity_level INTEGER,
    momentum_density INTEGER
);

-- Daily bars
CREATE TABLE IF NOT EXISTS daily_bars (
    symbol VARCHAR,
    trade_date DATE,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume BIGINT,
    vwap DOUBLE,
    previous_close DOUBLE,
    true_range DOUBLE,
    atr_14 DOUBLE,
    rolling_avg_volume_20d DOUBLE,
    rolling_avg_volume_50d DOUBLE,
    prior_day_gain_pct DOUBLE,
    multi_day_runner_flag BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (symbol, trade_date)
);

-- Minute bars
CREATE TABLE IF NOT EXISTS minute_bars (
    symbol VARCHAR,
    timestamp TIMESTAMP,
    session_date DATE,
    is_premarket BOOLEAN,
    is_regular_hours BOOLEAN,
    is_afterhours BOOLEAN,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume BIGINT,
    vwap DOUBLE,
    spread_pct DOUBLE,
    halt_status BOOLEAN DEFAULT FALSE,
    source_provider VARCHAR DEFAULT 'synthetic',
    quality_score DOUBLE,
    PRIMARY KEY (symbol, timestamp)
);

-- News / catalyst events
CREATE TABLE IF NOT EXISTS news_events (
    id VARCHAR,
    symbol VARCHAR,
    headline VARCHAR,
    source VARCHAR,
    published_at TIMESTAMP,
    catalyst_freshness_minutes DOUBLE,
    category VARCHAR,
    sentiment DOUBLE,
    is_earnings BOOLEAN DEFAULT FALSE,
    is_offering BOOLEAN DEFAULT FALSE,
    is_halt_related BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (id)
);

-- Halt events
CREATE TABLE IF NOT EXISTS halt_events (
    id VARCHAR,
    symbol VARCHAR,
    halt_start TIMESTAMP,
    halt_end TIMESTAMP,
    halt_type VARCHAR,
    price_at_halt DOUBLE,
    resumed_price DOUBLE,
    gap_on_resume_pct DOUBLE,
    PRIMARY KEY (id)
);

-- Market context (SPY/QQQ/VIX)
CREATE TABLE IF NOT EXISTS market_context (
    timestamp TIMESTAMP,
    trade_date DATE,
    spy_open DOUBLE,
    spy_high DOUBLE,
    spy_low DOUBLE,
    spy_close DOUBLE,
    spy_volume BIGINT,
    qqq_open DOUBLE,
    qqq_close DOUBLE,
    spy_intraday_return_pct DOUBLE,
    vix_open DOUBLE,
    vix_level DOUBLE,
    advance_decline_ratio DOUBLE,
    up_volume_down_volume_ratio DOUBLE,
    tape_regime_flag VARCHAR,
    PRIMARY KEY (timestamp)
);

-- Dilution / offering events
CREATE TABLE IF NOT EXISTS dilution_events (
    id VARCHAR PRIMARY KEY,
    symbol VARCHAR,
    event_type VARCHAR,
    announced_at TIMESTAMP,
    effective_date DATE,
    share_count BIGINT,
    price_per_share DOUBLE,
    gross_proceeds DOUBLE
);

-- Short interest history
CREATE TABLE IF NOT EXISTS short_interest (
    symbol VARCHAR,
    report_date DATE,
    short_interest_shares BIGINT,
    short_interest_pct_float DOUBLE,
    days_to_cover DOUBLE,
    PRIMARY KEY (symbol, report_date)
);

-- Versioned feature rows
CREATE TABLE IF NOT EXISTS engineered_features (
    id VARCHAR PRIMARY KEY,
    timestamp TIMESTAMP,
    symbol VARCHAR,
    session_date DATE,
    feature_version VARCHAR,
    gap_pct DOUBLE,
    premarket_gap_pct DOUBLE,
    relative_volume DOUBLE,
    time_of_day_adjusted_relative_volume DOUBLE,
    float_rotation_pct DOUBLE,
    vwap DOUBLE,
    distance_from_vwap DOUBLE,
    ema9 DOUBLE,
    ema20 DOUBLE,
    pullback_volume_ratio DOUBLE,
    pullback_candle_type VARCHAR,
    catalyst_freshness_minutes DOUBLE,
    catalyst_type VARCHAR,
    tape_regime VARCHAR,
    spy_intraday_return DOUBLE,
    vix_level DOUBLE,
    metadata_json VARCHAR
);

-- Setup events
CREATE TABLE IF NOT EXISTS setup_events (
    setup_id VARCHAR PRIMARY KEY,
    symbol VARCHAR,
    setup_time TIMESTAMP,
    session_date DATE,
    setup_name VARCHAR DEFAULT 'first_pullback',
    setup_version VARCHAR DEFAULT 'v1',
    entry_reference_price DOUBLE,
    invalidation_price DOUBLE,
    target_r_multiple DOUBLE DEFAULT 2.0,
    impulse_start_price DOUBLE,
    impulse_end_price DOUBLE,
    impulse_pct DOUBLE,
    pullback_low DOUBLE,
    pullback_depth_pct DOUBLE,
    pullback_bars INTEGER,
    pullback_volume_ratio DOUBLE,
    above_vwap_flag BOOLEAN,
    vwap_at_trigger DOUBLE,
    gap_pct DOUBLE,
    relative_volume DOUBLE,
    float_rotation_at_trigger DOUBLE,
    catalyst_freshness_at_trigger DOUBLE,
    session_minute_number INTEGER,
    tape_regime_at_trigger VARCHAR,
    spy_intraday_return_pct_at_trigger DOUBLE
);

-- Outcome labels
CREATE TABLE IF NOT EXISTS outcome_labels (
    setup_id VARCHAR PRIMARY KEY,
    label_version VARCHAR DEFAULT 'v1',
    max_upside_next_5m DOUBLE,
    max_upside_next_15m DOUBLE,
    max_upside_next_60m DOUBLE,
    max_drawdown_next_5m DOUBLE,
    max_drawdown_next_15m DOUBLE,
    max_drawdown_next_60m DOUBLE,
    reached_1r_before_minus_1r BOOLEAN,
    reached_2r_before_minus_1r BOOLEAN,
    held_vwap_5m BOOLEAN,
    held_vwap_15m BOOLEAN,
    trend_day_flag BOOLEAN,
    failed_breakout_flag BOOLEAN,
    time_to_max_upside_minutes DOUBLE,
    time_to_max_drawdown_minutes DOUBLE
);

-- Backtest runs
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
    win_rate DOUBLE,
    expectancy DOUBLE,
    max_drawdown DOUBLE,
    profit_factor DOUBLE
);

-- Simulated orders for paper trading
CREATE TABLE IF NOT EXISTS simulated_orders (
    order_id VARCHAR PRIMARY KEY,
    session_id VARCHAR,
    symbol VARCHAR,
    created_at TIMESTAMP,
    side VARCHAR,
    order_type VARCHAR,
    qty INTEGER,
    intended_entry DOUBLE,
    stop_price DOUBLE,
    target_price DOUBLE,
    status VARCHAR,
    strategy_name VARCHAR,
    setup_id VARCHAR
);

-- Simulated fills for paper trading
CREATE TABLE IF NOT EXISTS simulated_fills (
    fill_id VARCHAR PRIMARY KEY,
    order_id VARCHAR,
    symbol VARCHAR,
    fill_time TIMESTAMP,
    fill_price DOUBLE,
    qty INTEGER,
    slippage DOUBLE,
    commission DOUBLE,
    venue VARCHAR
);

-- Strategy run metadata for paper/live sessions
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

-- Daily paper trading session records
CREATE TABLE IF NOT EXISTS paper_sessions (
    session_id VARCHAR PRIMARY KEY,
    trade_date DATE,
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    starting_equity DOUBLE,
    ending_equity DOUBLE,
    realized_pnl DOUBLE,
    unrealized_pnl DOUBLE,
    total_orders INTEGER,
    total_fills INTEGER,
    total_setups INTEGER,
    notes VARCHAR
);

-- Live paper-trade missed-window / late-entry audit rows
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
    minutes_past_cutoff DOUBLE,
    success_score_pct DOUBLE,
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

-- Raw landing: one row per source+window fetch attempt (append-only)
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

-- Raw landing: one row per raw news item (append-only)
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

-- Telemetry events: structured observability for fetch attempts, state transitions, enrichment outcomes
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

-- Scanner Snapshots - Full scan state for historical tracking
CREATE TABLE IF NOT EXISTS scanner_snapshots (
    id VARCHAR PRIMARY KEY,
    snapshot_time TIMESTAMP,
    symbol VARCHAR,
    rank INTEGER,
    price DOUBLE,
    gap_pct DOUBLE,
    premarket_gap_pct DOUBLE,
    cumulative_volume BIGINT,
    minute_volume BIGINT,
    premarket_volume BIGINT,
    relative_volume DOUBLE,
    float_rotation_pct DOUBLE,
    vwap DOUBLE,
    distance_from_vwap DOUBLE,
    ema9 DOUBLE,
    ema20 DOUBLE,
    intraday_range DOUBLE,
    intraday_range_vs_atr DOUBLE,
    distance_to_premarket_high DOUBLE,
    distance_to_premarket_low DOUBLE,
    distance_to_day_high DOUBLE,
    distance_to_day_low DOUBLE,
    news_flag BOOLEAN,
    catalyst_freshness_minutes DOUBLE,
    catalyst_type VARCHAR,
    float_bucket VARCHAR,
    market_cap_bucket VARCHAR,
    spread_pct DOUBLE,
    halt_flag BOOLEAN,
    dilution_risk_flag BOOLEAN,
    sympathy_move_candidate_flag BOOLEAN,
    lead_runner_symbol VARCHAR,
    sympathy_cluster_id VARCHAR,
    early_leader_flag BOOLEAN,
    rank_stability DOUBLE,
    momentum_score DOUBLE,
    scanner_version VARCHAR
);
"""


def create_schema(con) -> None:
    """Create all tables in the DuckDB connection."""
    for statement in SCHEMA_SQL.split(";"):
        stmt = statement.strip()
        if stmt:
            con.execute(stmt)
    con.execute(
        "ALTER TABLE setup_events ADD COLUMN IF NOT EXISTS tape_regime_at_trigger VARCHAR"
    )
    con.execute(
        "ALTER TABLE setup_events ADD COLUMN IF NOT EXISTS spy_intraday_return_pct_at_trigger DOUBLE"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_raw_fetch_attempts_source_window "
        "ON raw_fetch_attempts (source, capability, request_window_start, request_window_end)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_raw_fetch_attempts_ingest_run "
        "ON raw_fetch_attempts (ingest_run_id)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_raw_news_items_fetch_attempt "
        "ON raw_news_items (fetch_attempt_id)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_raw_news_items_source_published "
        "ON raw_news_items (source, raw_published_at)"
    )
