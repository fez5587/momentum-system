"""Alpaca News API ingestion -> raw_news_items (the SAME landing table as RSS).

Benzinga-sourced headlines arrive with a stable ``id`` and an authoritative
``symbols`` list, so unlike rss.py we get real tickers (no regex scraping) and a
reliable dedupe key. Lands into raw_fetch_attempts + raw_news_items exactly like
rss.py, so the existing readers (recent_news_map catalyst prioritisation, the
Ollama enrichment in PR #2) pick it up with no other changes.

Stdlib-only transform; never raises — news is an enhancement, not a dependency.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PARSER_VERSION = "alpaca-news-v1"


@dataclass
class AlpacaNewsResult:
    source: str = "alpaca"
    fetch_attempt_id: str = ""
    item_count: int = 0
    error: str | None = None
    new_items: list[str] = field(default_factory=list)


def normalize_item(item: dict) -> dict:
    """One Alpaca news item -> raw_news_items row fields."""
    symbols = sorted({s for s in (item.get("symbols") or []) if s})
    return {
        "news_id": str(item.get("id") or ""),
        "url": item.get("url") or "",
        "title": (item.get("headline") or "").strip(),
        # created_at is the publish time; fall back to updated_at
        "published": item.get("created_at") or item.get("updated_at") or "",
        "snippet": (item.get("summary") or "")[:500],
        "tickers": ",".join(symbols),
        "src": item.get("source") or "alpaca",
    }


def _dedupe_hash(n: dict) -> str:
    """Stable per-item key: the Alpaca news id when present, else url+title."""
    basis = n["news_id"] or f"{n['url']}|{n['title']}"
    return hashlib.sha256(f"alpaca|{basis}".encode()).hexdigest()


def ingest_alpaca_news(
    con,
    client,
    symbols: list[str] | None = None,
    limit: int = 50,
    ingest_run_id: str | None = None,
) -> AlpacaNewsResult:
    """Fetch latest Alpaca news (optionally filtered to ``symbols``) and land new
    items into raw_news_items. Deduped by the Alpaca news id. Never raises."""
    result = AlpacaNewsResult()
    attempt_id = str(uuid.uuid4())
    result.fetch_attempt_id = attempt_id
    run_id = ingest_run_id or str(uuid.uuid4())
    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)

    try:
        items = client.get_news(symbols=symbols or None, limit=limit) or []
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
        items = []
        logger.warning("alpaca news fetch failed: %s", exc)

    try:
        con.execute(
            """
            INSERT INTO raw_fetch_attempts (
                id, source, capability, fetched_at, ingest_run_id,
                http_status, item_count, error_msg, payload_hash, parser_version
            ) VALUES (?, 'alpaca', 'news_api', ?, ?, ?, ?, ?, ?, ?)
            """,
            [attempt_id, fetched_at, run_id,
             200 if not result.error else None,
             len(items), result.error, None, PARSER_VERSION],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("alpaca news attempt-row insert failed: %s", exc)

    for item in items:
        n = normalize_item(item)
        if not n["news_id"] and not n["title"]:
            continue
        item_hash = _dedupe_hash(n)
        try:
            exists = con.execute(
                "SELECT 1 FROM raw_news_items WHERE payload_hash = ? LIMIT 1",
                [item_hash],
            ).fetchone()
            if exists:
                continue
            item_id = str(uuid.uuid4())
            con.execute(
                """
                INSERT INTO raw_news_items (
                    id, fetch_attempt_id, source, raw_url, raw_title,
                    raw_published_at, raw_body_snippet, raw_tickers,
                    payload_hash, parser_version, fetched_at, ingest_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [item_id, attempt_id, f"alpaca:{n['src']}", n["url"], n["title"],
                 n["published"], n["snippet"], n["tickers"], item_hash,
                 PARSER_VERSION, fetched_at, run_id],
            )
            result.new_items.append(item_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("alpaca news item insert failed: %s", exc)

    result.item_count = len(items)
    return result
