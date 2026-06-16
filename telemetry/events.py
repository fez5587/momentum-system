"""Telemetry event emission and querying for runtime observability."""

from __future__ import annotations

import json
import logging
import uuid
from enum import Enum

logger = logging.getLogger(__name__)


class TelemetryEventType(str, Enum):
    SOURCE_DEGRADED = "SOURCE_DEGRADED"
    SOURCE_RECOVERED = "SOURCE_RECOVERED"
    SOURCE_COOLDOWN = "SOURCE_COOLDOWN"
    WATCHLIST_ADD = "WATCHLIST_ADD"
    WATCHLIST_DROP = "WATCHLIST_DROP"
    CANDIDATE_STATE_CHANGE = "CANDIDATE_STATE_CHANGE"
    FETCH_ATTEMPT = "FETCH_ATTEMPT"
    ENRICHMENT_QUEUED = "ENRICHMENT_QUEUED"
    ENRICHMENT_DONE = "ENRICHMENT_DONE"
    ENRICHMENT_FAILED = "ENRICHMENT_FAILED"


def emit_event(
    con,
    event_type: TelemetryEventType | str,
    session_id: str | None = None,
    symbol: str | None = None,
    source: str | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
    reason: str | None = None,
    metadata: dict | None = None,
) -> None:
    event_id = str(uuid.uuid4())
    event_type_val = (
        event_type.value
        if isinstance(event_type, TelemetryEventType)
        else str(event_type)
    )
    metadata_json = json.dumps(metadata) if metadata is not None else None
    try:
        con.execute(
            """
            INSERT INTO telemetry_events
                (id, event_type, session_id, symbol, source, from_state, to_state, reason, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event_id,
                event_type_val,
                session_id,
                symbol,
                source,
                from_state,
                to_state,
                reason,
                metadata_json,
            ],
        )
    except Exception:
        logger.exception(
            "telemetry emit_event failed — event_type=%s symbol=%s",
            event_type_val,
            symbol,
        )


def query_events(
    con,
    session_id: str | None = None,
    symbol: str | None = None,
    source: str | None = None,
    limit: int = 100,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []

    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if symbol is not None:
        clauses.append("symbol = ?")
        params.append(symbol)
    if source is not None:
        clauses.append("source = ?")
        params.append(source)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM telemetry_events {where} ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = con.execute(sql, params).fetchall()
    cols = [
        desc[0]
        for desc in con.execute(
            f"SELECT * FROM telemetry_events {where} LIMIT 0", params[:-1]
        ).description
    ]
    return [dict(zip(cols, row)) for row in rows]
