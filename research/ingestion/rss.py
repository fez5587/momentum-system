"""RSS news ingestion: feed XML -> raw_fetch_attempts + raw_news_items.

Stdlib-only (urllib + xml.etree). Append-only raw landing tables; downstream
enrichment can dedupe via payload_hash.
"""

from __future__ import annotations

import hashlib
import re
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

PARSER_VERSION = "rss-v2"
TICKER_RE = re.compile(r"\((?:NASDAQ|NYSE|AMEX|OTC)[:\s]+([A-Z]{1,5})\)")


@dataclass
class RssIngestResult:
    source: str
    fetch_attempt_id: str = ""
    item_count: int = 0
    http_status: int | None = None
    error: str | None = None
    new_items: list[str] = field(default_factory=list)


def _text(element, tag: str) -> str:
    child = element.find(tag)
    return (child.text or "").strip() if child is not None else ""


def parse_rss(xml_bytes: bytes) -> list[dict]:
    """Parse RSS 2.0 / Atom into a list of raw item dicts."""
    root = ET.fromstring(xml_bytes)
    items: list[dict] = []
    # RSS 2.0
    for item in root.iter("item"):
        items.append(
            {
                "title": _text(item, "title"),
                "url": _text(item, "link"),
                "published": _text(item, "pubDate"),
                "snippet": _text(item, "description")[:500],
            }
        )
    # Atom
    ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.iter(f"{ns}entry"):
        link = entry.find(f"{ns}link")
        items.append(
            {
                "title": _text(entry, f"{ns}title"),
                "url": link.get("href", "") if link is not None else "",
                "published": _text(entry, f"{ns}updated"),
                "snippet": _text(entry, f"{ns}summary")[:500],
            }
        )
    return items


def extract_tickers(text: str) -> list[str]:
    return sorted(set(TICKER_RE.findall(text or "")))


def ingest_rss_feed(
    con,
    source: str,
    url: str,
    timeout: int = 15,
    ingest_run_id: str | None = None,
) -> RssIngestResult:
    """Fetch one RSS feed and land items into raw_news_items."""
    result = RssIngestResult(source=source)
    attempt_id = str(uuid.uuid4())
    result.fetch_attempt_id = attempt_id
    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
    payload: bytes = b""
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "momentum-research/1.0"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            result.http_status = resp.status
            payload = resp.read()
        items = parse_rss(payload)
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
        items = []
    payload_hash = hashlib.sha256(payload).hexdigest() if payload else None
    con.execute(
        """
        INSERT INTO raw_fetch_attempts (
            id, source, capability, fetched_at, ingest_run_id,
            http_status, item_count, error_msg, payload_hash, parser_version
        ) VALUES (?, ?, 'news_rss', ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            attempt_id,
            source,
            fetched_at,
            ingest_run_id,
            result.http_status,
            len(items),
            result.error,
            payload_hash,
            PARSER_VERSION,
        ],
    )
    for item in items:
        item_hash = hashlib.sha256(
            f"{source}|{item['url']}|{item['title']}".encode()
        ).hexdigest()
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
            [
                item_id,
                attempt_id,
                source,
                item["url"],
                item["title"],
                item["published"],
                item["snippet"],
                ",".join(extract_tickers(f"{item['title']} {item['snippet']}")),
                item_hash,
                PARSER_VERSION,
                fetched_at,
                ingest_run_id,
            ],
        )
        result.new_items.append(item_id)
    result.item_count = len(items)
    return result


def ingest_all_feeds(con, feeds: dict[str, str]) -> list[RssIngestResult]:
    """Ingest a {source_name: url} mapping; never raises."""
    run_id = str(uuid.uuid4())
    return [
        ingest_rss_feed(con, source, url, ingest_run_id=run_id)
        for source, url in feeds.items()
    ]
