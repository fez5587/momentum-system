-- YouTube claim-extraction pipeline schema (§7-8). Isolated in its own schema so it never
-- collides with the trading tables. {SCHEMA} is substituted at apply time (default: transcripts).

CREATE SCHEMA IF NOT EXISTS {SCHEMA};

-- config: which playlists to monitor
CREATE TABLE IF NOT EXISTS {SCHEMA}.playlists (
    playlist_id   TEXT PRIMARY KEY,
    label         TEXT,
    content_type  TEXT DEFAULT 'unknown',   -- analysis / news / opinion / promo / unknown
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- videos: one row per detected upload; dedupe key is video_id (§5)
CREATE TABLE IF NOT EXISTS {SCHEMA}.videos (
    video_id             TEXT PRIMARY KEY,
    playlist_id          TEXT REFERENCES {SCHEMA}.playlists(playlist_id),
    channel_id           TEXT,
    channel_name         TEXT,
    title                TEXT,
    content_type         TEXT DEFAULT 'unknown',
    published_at         TIMESTAMPTZ,
    detected_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    poll_latency_seconds INT,
    status               TEXT NOT NULL DEFAULT 'pending',  -- pending|transcribing|extracting|done|failed
    transcript_source    TEXT,                              -- whisper|captions|NULL
    whisper_model        TEXT,
    transcript_path      TEXT,
    processed_at         TIMESTAMPTZ,
    retry_count          INT NOT NULL DEFAULT 0,
    last_error           TEXT
);
CREATE INDEX IF NOT EXISTS videos_status_idx ON {SCHEMA}.videos(status);

-- claims: the core deliverable (§8) — DESCRIPTIVE only, verbatim_quote mandatory
CREATE TABLE IF NOT EXISTS {SCHEMA}.claims (
    claim_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    video_id              TEXT NOT NULL REFERENCES {SCHEMA}.videos(video_id),
    asset_ticker          TEXT,
    asset_name            TEXT,
    asset_class           TEXT,     -- equity|crypto|etf|index|commodity|fx|other
    direction             TEXT,     -- bullish|bearish|neutral|mixed (of the CLAIM, not advice)
    claim_text            TEXT,
    verbatim_quote        TEXT NOT NULL,   -- mandatory chain of custody back to source
    timestamp_start       NUMERIC,
    timestamp_end         NUMERIC,
    stated_rationale      TEXT,
    stated_horizon        TEXT,
    extraction_confidence NUMERIC,  -- 0-1 PARSE confidence, NOT truth confidence
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claims_video_idx  ON {SCHEMA}.claims(video_id);
CREATE INDEX IF NOT EXISTS claims_ticker_idx ON {SCHEMA}.claims(asset_ticker);

-- daily YouTube API quota counter (§5)
CREATE TABLE IF NOT EXISTS {SCHEMA}.api_quota (
    day        DATE PRIMARY KEY,
    units_used INT NOT NULL DEFAULT 0
);
