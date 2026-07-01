"""Postgres layer — shares the app's database (DATABASE_URL), isolated in the `transcripts`
schema. psycopg2 (consistent with the trading app). Videos are claimed by an atomic
status-flip under FOR UPDATE SKIP LOCKED, so the long transcription never holds a row lock."""

import os
from contextlib import contextmanager
from datetime import date, datetime, timezone

import psycopg2
import psycopg2.extras

from youtube_claims import config


def _dsn() -> str:
    return config.database_url()


@contextmanager
def connect():
    conn = psycopg2.connect(_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO {config.PG_SCHEMA}, public')
        yield conn
    finally:
        conn.close()


def apply_schema() -> None:
    ddl = open(os.path.join(os.path.dirname(__file__), "schema.sql")).read()
    ddl = ddl.replace("{SCHEMA}", config.PG_SCHEMA)
    with psycopg2.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()


# --- playlists ---
def upsert_playlist(playlist_id: str, label: str, content_type: str = "unknown") -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO playlists (playlist_id, label, content_type) VALUES (%s,%s,%s) "
                "ON CONFLICT (playlist_id) DO UPDATE SET label=EXCLUDED.label, "
                "content_type=EXCLUDED.content_type",
                (playlist_id, label, content_type))
        conn.commit()


def enabled_playlists() -> list[dict]:
    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM playlists WHERE enabled ORDER BY added_at")
            return [dict(r) for r in cur.fetchall()]


# --- videos ---
def seen_video_ids() -> set[str]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT video_id FROM videos")
            return {r[0] for r in cur.fetchall()}


def insert_pending_video(v: dict) -> bool:
    """Insert a newly-detected video as pending. Returns False if it already existed."""
    pub = v.get("published_at")
    lat = None
    if pub:
        lat = int((v["detected_at"] - pub).total_seconds())
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO videos (video_id, playlist_id, channel_id, channel_name, title, "
                "content_type, published_at, detected_at, poll_latency_seconds, status) "
                "VALUES (%(video_id)s,%(playlist_id)s,%(channel_id)s,%(channel_name)s,%(title)s,"
                "%(content_type)s,%(published_at)s,%(detected_at)s,%(lat)s,'pending') "
                "ON CONFLICT (video_id) DO NOTHING",
                {**v, "lat": lat})
            inserted = cur.rowcount == 1
        conn.commit()
        return inserted


def claim_next_pending() -> dict | None:
    """Atomically pick one pending video and flip it to `transcribing`. The lock is held
    only for the flip (SKIP LOCKED prevents two workers grabbing the same row); the long
    transcription runs AFTER the commit, holding no lock."""
    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM videos WHERE status='pending' "
                "ORDER BY published_at NULLS LAST, detected_at "
                "FOR UPDATE SKIP LOCKED LIMIT 1")
            row = cur.fetchone()
            if row is None:
                conn.commit()
                return None
            cur.execute("UPDATE videos SET status='transcribing' WHERE video_id=%s",
                        (row["video_id"],))
        conn.commit()
        return dict(row)


def set_status(video_id: str, status: str, **fields) -> None:
    cols = ", ".join(f"{k}=%s" for k in fields)
    params = list(fields.values()) + [video_id]
    sql = f"UPDATE videos SET status=%s{', ' + cols if cols else ''} WHERE video_id=%s"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [status, *params])
        conn.commit()


def record_failure(video_id: str, stage: str, error: str) -> str:
    """Bump retry_count and set last_error; return the resulting status
    (`pending` to retry, or `failed` once retries are exhausted)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT retry_count FROM videos WHERE video_id=%s", (video_id,))
            rc = (cur.fetchone() or [0])[0] + 1
            status = "pending" if rc <= config.MAX_RETRIES else "failed"
            cur.execute(
                "UPDATE videos SET status=%s, retry_count=%s, last_error=%s WHERE video_id=%s",
                (status, rc, f"[{stage}] {error}"[:2000], video_id))
        conn.commit()
        return status


def mark_done(video_id: str, transcript_source: str, whisper_model: str | None,
              transcript_path: str) -> None:
    set_status(video_id, "done", transcript_source=transcript_source,
               whisper_model=whisper_model, transcript_path=transcript_path,
               processed_at=datetime.now(timezone.utc))


# --- claims ---
def insert_claims(video_id: str, claims: list[dict]) -> int:
    if not claims:
        return 0
    fields = ["asset_ticker", "asset_name", "asset_class", "direction", "claim_text",
              "verbatim_quote", "timestamp_start", "timestamp_end", "stated_rationale",
              "stated_horizon", "extraction_confidence"]
    with connect() as conn:
        with conn.cursor() as cur:
            for c in claims:
                cur.execute(
                    f"INSERT INTO claims (video_id, {', '.join(fields)}) "
                    f"VALUES (%s, {', '.join(['%s'] * len(fields))})",
                    [video_id, *[c.get(f) for f in fields]])
        conn.commit()
    return len(claims)


# --- quota (§5) ---
def add_quota(units: int) -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_quota (day, units_used) VALUES (%s,%s) "
                "ON CONFLICT (day) DO UPDATE SET units_used = api_quota.units_used + EXCLUDED.units_used "
                "RETURNING units_used", (date.today(), units))
            used = cur.fetchone()[0]
        conn.commit()
        return used
